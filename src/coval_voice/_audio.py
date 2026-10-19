"""Pace outbound PCM16 audio at the relay sample rate."""

from __future__ import annotations

import asyncio
import time

from websockets.asyncio import client

from coval_voice import _defaults
from coval_voice import agent as agent_contract
from coval_voice import errors


async def send_paced_audio(
    websocket: client.ClientConnection,
    audio: bytes,
    *,
    next_send_at: float | None = None,
) -> float:
    """Send PCM16 in real-time chunks without flooding the worker.

    Args:
        websocket: Connected relay socket.
        audio: Even-length PCM16 mono 16 kHz bytes.
        next_send_at: Previous chunk's pacing deadline, or None for a new
            stream.

    Returns:
        The monotonic deadline for sending the next chunk.

    Raises:
        errors.TunnelError: Audio contains an incomplete PCM16 sample.
    """
    if len(audio) % _defaults.VOICE_SAMPLE_WIDTH_BYTES:
        raise errors.TunnelError(
            f"Agent returned {len(audio)} bytes of "
            f"PCM16 audio; an even byte count is "
            f"required."
        )
    if next_send_at is None:
        next_send_at = time.monotonic()
    for offset in range(0, len(audio), _defaults.VOICE_CHUNK_BYTES):
        chunk = audio[offset : offset + _defaults.VOICE_CHUNK_BYTES]
        await asyncio.sleep(max(0.0, next_send_at - time.monotonic()))
        await websocket.send(chunk)
        next_send_at = max(next_send_at, time.monotonic()) + len(chunk) / (
            _defaults.VOICE_SAMPLE_RATE_HZ
            * _defaults.VOICE_CHANNEL_COUNT
            * _defaults.VOICE_SAMPLE_WIDTH_BYTES
        )
    return next_send_at


class OutputAudio:
    """Own a session's bounded output queue and streaming producer.

    Attributes:
        sent_pcm_bytes: Number of bytes sent successfully to the relay.
    """

    def __init__(
        self,
        adapter: agent_contract.VoiceAgent | agent_contract.StreamingVoiceAgent,
        simulation_id: str,
    ) -> None:
        """Initialize output accounting for one conversation.

        Args:
            adapter: Fresh per-conversation callback or streaming agent.
            simulation_id: Identifier used in failure diagnostics.
        """
        self._agent = adapter
        self._simulation_id = simulation_id
        self.sent_pcm_bytes = 0
        self._pending_reply_bytes = 0
        self._replies: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=_defaults.MAX_PENDING_REPLIES
        )

    def queue_reply(self, audio: bytes) -> None:
        """Enqueue a callback reply while counting queued and in-flight bytes.

        Args:
            audio: Callback output to send after readiness.

        Raises:
            errors.TunnelError: The byte or reply-count budget is exhausted.
        """
        if (
            self._pending_reply_bytes + len(audio)
            > _defaults.MAX_PENDING_REPLY_BYTES
            or self._replies.full()
        ):
            raise errors.TunnelError(
                f"Simulation {self._simulation_id} "
                f"agent replies exceeded the "
                f"bounded audio send queue."
            )
        self._pending_reply_bytes += len(audio)
        self._replies.put_nowait(audio)

    async def _send_replies(self, websocket: client.ClientConnection) -> None:
        while True:
            audio = await self._replies.get()
            await send_paced_audio(websocket, audio)
            self.sent_pcm_bytes += len(audio)
            self._pending_reply_bytes -= len(audio)

    async def _send_stream(self, websocket: client.ClientConnection) -> None:
        if not isinstance(self._agent, agent_contract.StreamingVoiceAgent):
            raise TypeError("Streaming audio requires a StreamingVoiceAgent.")
        stream = self._agent.audio_output()
        next_send_at: float | None = None
        try:
            async for chunk in stream:
                audio = _validate_chunk(chunk)
                if len(audio) > _defaults.MAX_PENDING_REPLY_BYTES:
                    raise errors.TunnelError(
                        "Agent output chunk exceeds the 2 MiB limit; "
                        "yield smaller chunks."
                    )
                if audio:
                    next_send_at = await send_paced_audio(
                        websocket, audio, next_send_at=next_send_at
                    )
                    self.sent_pcm_bytes += len(audio)
                else:
                    await asyncio.sleep(0)
            raise errors.TunnelError(
                f"Simulation {self._simulation_id} agent "
                f"audio stream ended before session_end."
            )
        finally:
            close_stream = getattr(stream, "aclose", None)
            if close_stream is not None:
                await close_stream()

    async def send(
        self, websocket: client.ClientConnection, ready: asyncio.Event
    ) -> None:
        """Start the selected output producer after agent readiness.

        Args:
            websocket: Connected relay socket owned by the session.
            ready: Event set after the agent finishes on_ready().
        """
        await ready.wait()
        if isinstance(self._agent, agent_contract.StreamingVoiceAgent):
            await self._send_stream(websocket)
        else:
            await self._send_replies(websocket)


def _validate_chunk(audio: object) -> bytes:
    """Reject malformed output from adapters that violate the typed contract."""
    if (
        not isinstance(audio, bytes)
        or len(audio) % _defaults.VOICE_SAMPLE_WIDTH_BYTES
    ):
        raise errors.TunnelError(
            "Agent output must be even-length PCM16 bytes."
        )
    return audio
