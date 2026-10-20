"""Own relay contexts, application handlers, and trace finalization."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from collections.abc import Awaitable
from collections.abc import Callable
import contextlib
import json
import time
from typing import Any
from typing import cast
from urllib import parse

from websockets import exceptions

from coval_agent_sdk import _connection
from coval_agent_sdk import _defaults
from coval_agent_sdk import _exceptions
from coval_agent_sdk import _http
from coval_agent_sdk import audio
from coval_agent_sdk import errors
from coval_agent_sdk import results
from coval_agent_sdk import telemetry


@contextlib.asynccontextmanager
async def open_session(
    api: _http.CovalApi,
    simulation_id: str,
    trace_exporter: telemetry.TraceExporter | None = None,
) -> AsyncGenerator[audio.AudioConnection, None]:
    """Yield a ready connection and finalize it after application cleanup."""
    session = _Session(api, simulation_id, trace_exporter)
    primary_error: BaseException | None = None
    try:
        await session.start()
        yield session.connection
        if session.state.error is not None:
            raise session.state.error
        if not session.state.completed:
            raise errors.TunnelError(
                f"Simulation {simulation_id} audio context exited "
                "before the conversation completed."
            )
    except BaseException as exception:
        primary_error = exception
        raise
    finally:
        await session.close(primary_error)


async def run_session(
    api: _http.CovalApi,
    simulation_id: str,
    on_connection: Callable[[audio.AudioConnection], Awaitable[None]],
    trace_exporter: telemetry.TraceExporter | None = None,
) -> results.SessionResult:
    """Run an application handler while observing transport failures."""
    async with open_session(api, simulation_id, trace_exporter) as connection:
        handling = asyncio.ensure_future(on_connection(connection))
        closing = asyncio.create_task(connection.wait_closed())
        try:
            done, _ = await asyncio.wait(
                (handling, closing), return_when=asyncio.FIRST_COMPLETED
            )
            if closing in done:
                await closing
            await handling
        finally:
            for task in (handling, closing):
                if not task.done():
                    task.cancel()
            await asyncio.gather(handling, closing, return_exceptions=True)
    if connection.result is None:
        raise errors.TunnelError(
            "The audio connection has no completed result."
        )
    return connection.result


class _Session:
    """Own the receive task and finalize traces after application work ends."""

    def __init__(
        self,
        api: _http.CovalApi,
        simulation_id: str,
        trace_exporter: telemetry.TraceExporter | None,
    ) -> None:
        self._api = api
        self.connection = audio.AudioConnection(simulation_id)
        # The context and public handle share private transport state.
        self.state = (
            self.connection._state  # pyright: ignore[reportPrivateUsage]
        )
        self._trace_exporter = trace_exporter
        self._reader: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Wait for validated readiness or fail before yielding a connection."""
        self._reader = asyncio.create_task(self._read())
        ready = asyncio.create_task(self.state.ready.wait())
        try:
            await asyncio.wait(
                (ready, self._reader), return_when=asyncio.FIRST_COMPLETED
            )
            if self.state.error is not None:
                raise self.state.error
            if not self.state.ready.is_set():
                raise errors.TunnelError(
                    "The relay closed before session_ready."
                )
        finally:
            ready.cancel()
            await asyncio.gather(ready, return_exceptions=True)

    async def _read(self) -> None:
        """Keep relay reception independent of application audio processing."""
        failure: BaseException | None = None
        try:
            await self._exchange()
        except exceptions.ConnectionClosed as exception:
            close_code = exception.rcvd.code if exception.rcvd else None
            if (
                self.state.ready.is_set()
                and close_code in {None, 1000, 1001}
                and await _confirm_completed_after_close(
                    self._api, self.connection.simulation_id
                )
            ):
                self.state.completed = True
            else:
                failure = errors.TunnelError(
                    f"Simulation {self.connection.simulation_id} relay closed "
                    f"without completion (code {close_code})."
                )
                failure.__cause__ = exception
        except BaseException as exception:
            failure = exception
        finally:
            self.state.finish(failure)

    async def _exchange(self) -> None:
        """Open one outbound socket and validate incoming control/audio."""
        connection = self.connection
        websocket = await _connection.open_voice_connection(
            self._api, connection.simulation_id
        )
        async with websocket:
            self.state.websocket = websocket
            messages = aiter(websocket)
            deadline = time.monotonic() + _defaults.CONNECT_WAIT_SECONDS
            while True:
                try:
                    if self.state.ready.is_set():
                        message = await anext(messages)
                    else:
                        message = await asyncio.wait_for(
                            anext(messages),
                            max(0.0, deadline - time.monotonic()),
                        )
                except asyncio.TimeoutError as exception:
                    raise errors.TunnelError(
                        "Voice relay did not send session_ready within "
                        f"{_defaults.CONNECT_WAIT_SECONDS} seconds."
                    ) from exception
                except StopAsyncIteration:
                    break
                if isinstance(message, str):
                    self._control(message)
                    if self.state.completed:
                        return
                else:
                    if not self.state.ready.is_set():
                        raise errors.TunnelError(
                            "Audio arrived before session_ready."
                        )
                    if len(message) % _defaults.VOICE_SAMPLE_WIDTH_BYTES:
                        raise errors.TunnelError(
                            "Incoming PCM16 requires even bytes."
                        )
                    self.state.feed_audio(message)
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "audio_ack",
                                "received_bytes": self.state.received_bytes,
                            }
                        )
                    )
        if self.state.ready.is_set() and await _confirm_completed_after_close(
            self._api, connection.simulation_id
        ):
            self.state.completed = True
            return
        raise errors.TunnelError("Voice relay closed before session_end.")

    def _control(self, message: str) -> None:
        """Require one valid readiness frame before successful completion."""
        try:
            control = json.loads(message)
        except json.JSONDecodeError:
            raise errors.TunnelError(
                "Relay returned invalid control JSON."
            ) from None
        if not isinstance(control, dict):
            raise errors.TunnelError("Relay returned invalid control JSON.")
        control = cast(dict[str, Any], control)
        message_type = control.get("type")
        if message_type == "session_ready":
            if self.state.ready.is_set():
                raise errors.TunnelError("Relay repeated session_ready.")
            if control.get("audio") != {
                "encoding": "pcm_s16le",
                "sample_rate_hz": _defaults.VOICE_SAMPLE_RATE_HZ,
                "channels": _defaults.VOICE_CHANNEL_COUNT,
            }:
                raise errors.TunnelError(
                    "Relay requires an unsupported audio format."
                )
            self.state.ready.set()
        elif message_type == "session_end":
            if not self.state.ready.is_set():
                raise errors.TunnelError("Relay ended before session_ready.")
            self.state.completed = True
            self.state.finish()
        elif message_type == "error":
            raise errors.TunnelError(f"Relay error: {control.get('message')}")

    async def close(self, primary_error: BaseException | None) -> None:
        """Release transport and traces while preserving application errors."""
        if self._reader is not None:
            if not self._reader.done() and (
                primary_error is not None or not self.state.completed
            ):
                self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        transport_error = self.state.error if primary_error is None else None
        successful = (
            self.state.completed
            and primary_error is None
            and transport_error is None
        )
        trace_result = await self._finalize_trace(
            successful,
            primary_error if primary_error is not None else transport_error,
        )
        if transport_error is not None:
            raise transport_error
        if successful:
            self.state.result = results.SessionResult(
                simulation_id=self.connection.simulation_id,
                received_audio_bytes=self.state.received_bytes,
                sent_audio_bytes=self.state.writer.sent_bytes,
                trace=trace_result,
            )

    async def _finalize_trace(
        self, successful: bool, primary_error: BaseException | None
    ) -> results.TraceResult | None:
        """Flush only after the caller has stopped its pipeline and spans."""
        exporter = self._trace_exporter
        if exporter is None:
            return None
        cleanup_error: BaseException | None = None
        outcome: results.TraceResult | None = None
        try:
            if not await asyncio.to_thread(
                exporter.flush,
                timeout_millis=_defaults.TRACE_FLUSH_TIMEOUT_MILLISECONDS,
            ):
                raise errors.CovalTraceError(
                    "Trace exporter could not flush spans."
                )
            if successful:
                count = await asyncio.to_thread(
                    exporter.upload,
                    simulation_id=self.connection.simulation_id,
                    api_base_url=self._api.api_base_url,
                    api_key=self._api.api_key,
                    workspace_id=self._api.workspace_id,
                )
                outcome = results.TraceResult(uploaded_spans=count)
        except BaseException as exception:
            if primary_error is not None:
                _exceptions.add_cleanup_note(
                    primary_error, f"Trace cleanup also failed: {exception!r}"
                )
            elif successful and isinstance(exception, errors.CovalTraceError):
                outcome = results.TraceResult(error=str(exception))
            else:
                cleanup_error = exception
        finally:
            if not successful:
                try:
                    exporter.discard(
                        simulation_id=self.connection.simulation_id
                    )
                except BaseException as exception:
                    primary = (
                        primary_error
                        if primary_error is not None
                        else cleanup_error
                    )
                    if primary is None:
                        raise
                    _exceptions.add_cleanup_note(
                        primary, f"Trace discard also failed: {exception!r}"
                    )
        if cleanup_error is not None:
            raise cleanup_error
        return outcome


async def _confirm_completed_after_close(
    api: _http.CovalApi, simulation_id: str
) -> bool:
    """Accept a lost terminal frame only after confirmed completion."""
    deadline = time.monotonic() + _defaults.CLOSE_CONFIRM_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        remaining_seconds = deadline - time.monotonic()
        try:
            response = await asyncio.wait_for(
                api.get(f"simulations/{parse.quote(simulation_id, safe='')}"),
                timeout=remaining_seconds,
            )
        except (errors.TunnelError, json.JSONDecodeError, asyncio.TimeoutError):
            return False
        simulation = response.get("simulation")
        if not isinstance(simulation, dict):
            return False
        simulation = cast(dict[str, Any], simulation)
        status = simulation.get("status")
        if status == "COMPLETED":
            return True
        if status not in {"PENDING", "QUEUED", "IN PROGRESS"}:
            return False
        await asyncio.sleep(
            max(
                0.0,
                min(
                    _defaults.POLL_INTERVAL_SECONDS, deadline - time.monotonic()
                ),
            )
        )
    return False
