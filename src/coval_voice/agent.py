"""Contracts implemented by per-conversation voice adapters."""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from collections.abc import Callable
from typing import Protocol
from typing import runtime_checkable


@runtime_checkable
class VoiceAgent(Protocol):
    """Consume and optionally emit PCM16 mono 16 kHz audio."""

    async def on_ready(self) -> bytes | None:
        """Return optional greeting audio after the relay is ready.

        Returns:
            PCM16 little-endian, mono, 16 kHz greeting bytes, or None.
        """

    async def on_audio(self, audio: bytes) -> bytes | None:
        """Enqueue incoming audio promptly and return an optional reply.

        This callback blocks reception of the next frame. Slow ASR or provider
        work belongs in a background task with a bounded queue. Sustained
        processing must keep pace with 20 ms input chunks; the relay rejects a
        64 KiB backlog.

        Args:
            audio: Even-length PCM16 little-endian, mono, 16 kHz bytes.

        Returns:
            Reply bytes in the same format, or None when there is no reply.
        """

    async def close(self) -> None:
        """Release resources after the socket closes."""


class StreamingVoiceAgent(abc.ABC):
    """Produce PCM independently of incoming audio using a bounded async stream.

    Subclass this adapter for agents whose TTS runs in background tasks. The SDK
    pulls output only after on_ready completes and paces it in 20 ms chunks.
    Keep your producer queue bounded; an output error fails the session and
    calls close.
    """

    async def on_ready(self) -> None:
        """Start the agent after the relay is ready, returning promptly."""
        return

    @abc.abstractmethod
    async def on_audio(self, audio: bytes) -> None:
        """Enqueue incoming audio without waiting for remote processing.

        Args:
            audio: Even-length PCM16 little-endian, mono, 16 kHz bytes.
        """

    @abc.abstractmethod
    def audio_output(self) -> AsyncIterator[bytes]:
        """Produce speech independently of incoming audio, including greetings.

        Keep yielding until the SDK cancels the session. The SDK starts
        consuming after on_ready() completes. An iterator that ends early fails
        the session.

        Yields:
            Even-length PCM16 little-endian, mono, 16 kHz bytes. Empty chunks
            are ignored; each nonempty chunk must be at most 2 MiB. Bound the
            producer queue and prefer 20 ms chunks.
        """

    @abc.abstractmethod
    async def close(self) -> None:
        """Stop agent tasks and finish conversation spans."""


AgentFactory = Callable[[str], VoiceAgent | StreamingVoiceAgent]
