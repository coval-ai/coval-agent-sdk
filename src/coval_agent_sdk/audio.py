"""Framework-independent audio connection for one Coval conversation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from websockets import exceptions

from coval_agent_sdk import _audio
from coval_agent_sdk import errors
from coval_agent_sdk import results


class AudioConnection:
    """Exchange PCM16 little-endian, mono, 16 kHz audio with a simulation.

    Obtain ready connections through CovalVoiceClient.connect() or a run's
    on_connection handler. The application owns its pipeline and tasks; the
    SDK owns this connection. Use one receive loop per connection. Output can
    be sent independently, including before any caller audio arrives.

    Attributes:
        simulation_id: Hosted conversation identifier for correlation.
    """

    def __init__(self, simulation_id: str) -> None:
        """Initialize local state without opening a connection.

        Args:
            simulation_id: Identifier supplied by the client.
        """
        self._state = _audio.ConnectionState(simulation_id)

    @property
    def simulation_id(self) -> str:
        """Hosted conversation identifier for application correlation."""
        return self._state.simulation_id

    @property
    def closed(self) -> bool:
        """Whether the audio exchange has ended, successfully or otherwise."""
        return self._state.closed.is_set()

    @property
    def result(self) -> results.SessionResult | None:
        """Completed outcome after context exit, or None before finalization."""
        return self._state.result

    async def receive_audio(self) -> AsyncIterator[bytes]:
        """Yield caller audio until the conversation ends.

        Drain promptly into the framework's bounded pipeline. Only one receive
        iterator may be active. A failed connection raises its original error;
        it does not appear as a successful end of input.

        Yields:
            Even-length PCM16 mono 16 kHz bytes in arrival order.

        Raises:
            RuntimeError: Another receive iterator is already active.
            errors.CovalError: The relay or receive buffer fails.
        """
        if self._state.receiving:
            raise RuntimeError(
                "Only one audio receiver may consume a connection."
            )
        self._state.receiving = True
        try:
            while True:
                if self._state.error is not None:
                    raise self._state.error
                if self._state.input:
                    audio = self._state.input.popleft()
                    self._state.buffered_bytes -= len(audio)
                    yield audio
                elif self.closed:
                    return
                else:
                    self._state.input_changed.clear()
                    await self._state.input_changed.wait()
        finally:
            self._state.receiving = False

    async def send_audio(self, audio: bytes) -> bool:
        """Send audio at real-time speed with bounded, serialized writes.

        Await each write instead of creating an unbounded set of send tasks.
        Concurrent writes are serialized and retain pacing across calls.

        Args:
            audio: Even-length bytes, at most 2 MiB. Empty bytes are allowed.

        Returns:
            True if all bytes were sent; False if normal conversation completion
            interrupted the write. A partial write is never replayed.

        Raises:
            errors.CovalError: Audio is invalid or the connection fails.
        """
        _audio.validate_audio(audio)
        if self._state.error is not None:
            raise self._state.error
        if self.closed:
            return False
        if self._state.websocket is None or not self._state.ready.is_set():
            raise errors.TunnelError("The audio connection is not ready.")
        sending = asyncio.create_task(
            self._state.writer.send(self._state.websocket, audio)
        )
        closing = asyncio.create_task(self._state.closed.wait())
        try:
            done, _ = await asyncio.wait(
                (sending, closing), return_when=asyncio.FIRST_COMPLETED
            )
            if self._state.error is not None:
                raise self._state.error
            if sending in done:
                try:
                    await sending
                except exceptions.ConnectionClosed:
                    # The reader owns authoritative completion after closure.
                    await self.wait_closed()
                    return False
                except OSError as exception:
                    failure = errors.CovalConnectionError(
                        "The relay audio write failed."
                    )
                    self._state.finish(failure)
                    raise failure from exception
                return True
            return False
        finally:
            for task in (sending, closing):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sending, closing, return_exceptions=True)

    async def wait_closed(self) -> None:
        """Wait for voice completion, independently of hosted evaluation.

        Raises:
            errors.CovalError: The connection failed instead of completing.
        """
        await self._state.closed.wait()
        if self._state.error is not None:
            raise self._state.error
