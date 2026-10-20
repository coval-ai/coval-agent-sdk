"""Validate and pace serialized outbound PCM16 audio."""

from __future__ import annotations

import asyncio
from collections import deque
import time

from websockets.asyncio import client

from coval_agent_sdk import _defaults
from coval_agent_sdk import errors
from coval_agent_sdk import results


def validate_audio(audio: object) -> None:
    """Reject invalid or oversized application audio before any wire write.

    Args:
        audio: PCM16 little-endian mono 16 kHz bytes.

    Raises:
        errors.TunnelError: Audio is malformed or exceeds the 2 MiB limit.
    """
    if (
        not isinstance(audio, bytes)
        or len(audio) % _defaults.VOICE_SAMPLE_WIDTH_BYTES
    ):
        raise errors.TunnelError("Audio must be even-length PCM16 bytes.")
    if len(audio) > _defaults.MAX_SEND_BYTES:
        raise errors.TunnelError(
            "Audio exceeds the 2 MiB limit; send smaller chunks."
        )


class PcmWriter:
    """Own output serialization, cross-call pacing, and exact sent counts."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._next_send_at = 0.0
        self.sent_bytes = 0

    async def send(
        self, websocket: client.ClientConnection, audio: bytes
    ) -> None:
        """Send validated chunks while retaining pacing between callers."""
        async with self._lock:
            for offset in range(0, len(audio), _defaults.VOICE_CHUNK_BYTES):
                chunk = audio[offset : offset + _defaults.VOICE_CHUNK_BYTES]
                await asyncio.sleep(
                    max(0.0, self._next_send_at - time.monotonic())
                )
                await websocket.send(chunk)
                self.sent_bytes += len(chunk)
                self._next_send_at = max(
                    self._next_send_at, time.monotonic()
                ) + len(chunk) / (
                    _defaults.VOICE_SAMPLE_RATE_HZ
                    * _defaults.VOICE_CHANNEL_COUNT
                    * _defaults.VOICE_SAMPLE_WIDTH_BYTES
                )


class ConnectionState:
    """Private shared transport state; never exported by the public facade."""

    def __init__(self, simulation_id: str) -> None:
        self.simulation_id = simulation_id
        self.websocket: client.ClientConnection | None = None
        self.writer = PcmWriter()
        self.input: deque[bytes] = deque()
        self.buffered_bytes = 0
        self.input_changed = asyncio.Event()
        self.receiving = False
        self.ready = asyncio.Event()
        self.closed = asyncio.Event()
        self.error: BaseException | None = None
        self.completed = False
        self.received_bytes = 0
        self.result: results.SessionResult | None = None

    def feed_audio(self, audio: bytes) -> None:
        """Bound unread audio without blocking terminal control frames."""
        if self.buffered_bytes + len(audio) > _defaults.MAX_RECEIVE_BYTES:
            raise errors.TunnelError(
                "Unread audio exceeded the 64 KiB receive buffer; "
                "drain receive_audio() promptly into a bounded pipeline."
            )
        self.received_bytes += len(audio)
        self.buffered_bytes += len(audio)
        if audio:
            self.input.append(audio)
            self.input_changed.set()

    def finish(self, error: BaseException | None = None) -> None:
        """Wake readers and writers without manufacturing success."""
        if self.error is None:
            self.error = error
        self.closed.set()
        self.input_changed.set()
