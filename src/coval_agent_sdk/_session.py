"""Own one agent, relay session, cancellation, and terminal trace cleanup."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
import time
from typing import Any
from typing import cast
from urllib import parse

from websockets import exceptions
from websockets.asyncio import client

from coval_agent_sdk import _audio
from coval_agent_sdk import _connection
from coval_agent_sdk import _defaults
from coval_agent_sdk import _http
from coval_agent_sdk import agent as agent_contract
from coval_agent_sdk import errors
from coval_agent_sdk import results
from coval_agent_sdk import telemetry


def _add_cleanup_note(exception: BaseException, note: str) -> None:
    """Attach cleanup context on runtimes supporting exception notes."""
    add_note = getattr(exception, "add_note", None)
    if callable(add_note):
        add_note(note)


async def run_session(
    api: _http.CovalApi,
    simulation_id: str,
    agent_factory: agent_contract.AgentFactory,
    trace_exporter: telemetry.TraceExporter | None = None,
) -> results.SessionResult:
    """Connect one simulation with a fresh agent and guaranteed local cleanup.

    Args:
        api: Authenticated API transport for the selected workspace.
        simulation_id: Hosted conversation identifier.
        agent_factory: Factory called exactly once for this local session.
        trace_exporter: Optional exporter shared with the agent's pipeline.

    Raises:
        errors.TunnelError: Relay discovery or the session protocol fails.
        Exception: Agent or transport work fails; cleanup preserves that error.
    """
    return await _Session(
        api, simulation_id, agent_factory, trace_exporter
    ).run()


class _Session:
    """Own one agent, connection task, and terminal cleanup sequence."""

    def __init__(
        self,
        api: _http.CovalApi,
        simulation_id: str,
        agent_factory: agent_contract.AgentFactory,
        trace_exporter: telemetry.TraceExporter | None,
    ) -> None:
        self._api = api
        self._simulation_id = simulation_id
        self._agent = agent_factory(simulation_id)
        self._trace_exporter = trace_exporter
        self._output = _audio.OutputAudio(self._agent, simulation_id)
        self._received_pcm_bytes = 0
        self._sender: asyncio.Task[None] | None = None
        self._trace_result: results.TraceResult | None = None
        self._completed = False
        self._session_started = False

    async def run(self) -> results.SessionResult:
        """Run the exchange, preserving the primary error through cleanup."""
        session_error: BaseException | None = None
        try:
            await self._exchange()
        except exceptions.ConnectionClosed as exception:
            session_error = exception
            if exception.rcvd is not None and exception.rcvd.code == 1013:
                session_error = errors.TunnelError(
                    f"Simulation {self._simulation_id} exceeded "
                    f"the voice relay's audio backlog limit. "
                    f"Keep on_audio callbacks fast by "
                    f"enqueueing audio for background "
                    f"processing in a bounded queue."
                )
                raise session_error from exception
            close_code = (
                exception.rcvd.code if exception.rcvd is not None else None
            )
            if (
                self._session_started
                and close_code in {None, 1000, 1001}
                and await _confirm_completed_after_close(
                    self._api, self._simulation_id
                )
            ):
                self._completed = True
                session_error = None
            else:
                session_error = exception
                raise
        except BaseException as exception:
            session_error = exception
            raise
        finally:
            await self._cleanup(session_error)
        return results.SessionResult(
            simulation_id=self._simulation_id,
            received_audio_bytes=self._received_pcm_bytes,
            sent_audio_bytes=self._output.sent_pcm_bytes,
            trace=self._trace_result,
        )

    async def _exchange(self) -> None:
        """Own the relay context until terminal control or socket closure."""
        connection = await _connection.open_voice_connection(
            self._api, self._simulation_id
        )
        async with connection:
            websocket = connection
            ready = asyncio.Event()
            self._sender = asyncio.create_task(
                self._output.send(websocket, ready)
            )
            async for message in receive_while_sending(
                websocket, self._sender, ready_event=ready
            ):
                if isinstance(message, str):
                    await self._handle_control(message, ready)
                    if self._completed:
                        return
                else:
                    await self._handle_audio(websocket, message, ready)
            if self._session_started and await _confirm_completed_after_close(
                self._api, self._simulation_id
            ):
                self._completed = True
                return
            raise errors.TunnelError(
                f"Simulation {self._simulation_id} closed its "
                f"voice relay before session_end."
            )

    async def _handle_control(self, message: str, ready: asyncio.Event) -> None:
        """Validate control messages before changing session state."""
        control = json.loads(message)
        if not isinstance(control, dict):
            raise errors.TunnelError(
                f"Simulation {self._simulation_id} returned "
                f"invalid control JSON."
            )
        control = cast(dict[str, Any], control)
        message_type = control.get("type")
        if message_type == "session_ready":
            audio_format = control.get("audio")
            expected_format = {
                "encoding": "pcm_s16le",
                "sample_rate_hz": _defaults.VOICE_SAMPLE_RATE_HZ,
                "channels": _defaults.VOICE_CHANNEL_COUNT,
            }
            if audio_format != expected_format:
                raise errors.TunnelError(
                    f"Simulation {self._simulation_id} requires "
                    f"unsupported audio format "
                    f"{audio_format!r}; expected "
                    f"{expected_format!r}."
                )
            greeting = await self._agent.on_ready()
            self._session_started = True
            ready.set()
            if greeting:
                self._output.queue_reply(greeting)
        elif message_type == "session_end":
            self._completed = True
            return
        elif message_type == "error":
            raise errors.TunnelError(
                f"Simulation {self._simulation_id} reported an "
                f"error: {control.get('message')}"
            )

    async def _handle_audio(
        self,
        websocket: client.ClientConnection,
        message: bytes,
        ready: asyncio.Event,
    ) -> None:
        """Acknowledge cumulative input bytes before calling the agent."""
        if not ready.is_set():
            raise errors.TunnelError(
                f"Simulation {self._simulation_id} sent audio "
                f"before session_ready."
            )
        if len(message) % _defaults.VOICE_SAMPLE_WIDTH_BYTES:
            raise errors.TunnelError(
                f"Simulation {self._simulation_id} sent "
                f"{len(message)} PCM16 bytes; an even "
                f"byte count is required."
            )
        self._received_pcm_bytes += len(message)
        await websocket.send(
            json.dumps(
                {
                    "type": "audio_ack",
                    "received_bytes": self._received_pcm_bytes,
                }
            )
        )
        reply = await self._agent.on_audio(message)
        if reply:
            self._output.queue_reply(reply)

    async def _cleanup(self, session_error: BaseException | None) -> None:
        """Stop output, close the agent, then finalize traces."""
        cleanup_error: BaseException | None = None
        if self._sender is not None:
            self._sender.cancel()
            await asyncio.gather(self._sender, return_exceptions=True)
        try:
            await self._agent.close()
        except BaseException as exception:
            if session_error is not None:
                _add_cleanup_note(
                    session_error, f"Agent cleanup also failed: {exception!r}"
                )
            else:
                cleanup_error = exception
        if self._trace_exporter is not None:
            try:
                if not await asyncio.to_thread(
                    self._trace_exporter.flush,
                    timeout_millis=_defaults.TRACE_FLUSH_TIMEOUT_MILLISECONDS,
                ):
                    raise errors.CovalTraceError(
                        f"Trace exporter could not flush spans "
                        f"for simulation {self._simulation_id}."
                    )
                if self._completed:
                    span_count = await asyncio.to_thread(
                        self._trace_exporter.upload,
                        simulation_id=self._simulation_id,
                        api_base_url=self._api.api_base_url,
                        api_key=self._api.api_key,
                        workspace_id=self._api.workspace_id,
                    )
                    self._trace_result = results.TraceResult(
                        uploaded_spans=span_count
                    )
            except BaseException as exception:
                primary_error = (
                    session_error
                    if session_error is not None
                    else cleanup_error
                )
                if primary_error is not None:
                    _add_cleanup_note(
                        primary_error,
                        f"Trace cleanup also failed: {exception!r}",
                    )
                elif self._completed and isinstance(
                    exception, errors.CovalTraceError
                ):
                    self._trace_result = results.TraceResult(
                        error=str(exception)
                    )
                else:
                    cleanup_error = exception
            finally:
                if not self._completed:
                    self._trace_exporter.discard(
                        simulation_id=self._simulation_id
                    )
        if cleanup_error is not None:
            raise cleanup_error


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


async def receive_while_sending(
    websocket: client.ClientConnection,
    sender: asyncio.Task[None],
    *,
    ready_event: asyncio.Event | None = None,
) -> AsyncIterator[str | bytes]:
    """Keep receiving audio during paced replies and surface sender failures."""
    messages = aiter(websocket)
    ready_deadline = time.monotonic() + _defaults.CONNECT_WAIT_SECONDS
    while True:
        receive = asyncio.ensure_future(anext(messages))
        try:
            readiness_pending = (
                ready_event is not None and not ready_event.is_set()
            )
            timeout_seconds = (
                max(0, ready_deadline - time.monotonic())
                if readiness_pending
                else None
            )
            done, _ = await asyncio.wait(
                (receive, sender),
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise errors.TunnelError(
                    f"Voice relay did not send session_ready "
                    f"within "
                    f"{_defaults.CONNECT_WAIT_SECONDS} "
                    f"seconds."
                )
            if sender in done:
                await sender
            yield await receive
        except StopAsyncIteration:
            return
        finally:
            if not receive.done():
                receive.cancel()
            await asyncio.gather(receive, return_exceptions=True)
