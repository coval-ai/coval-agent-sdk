"""Validate, bound, and serialize text messages on one relay connection."""

from __future__ import annotations

import asyncio
import collections
import json

from websockets.asyncio import client

from coval_agent_sdk import _defaults
from coval_agent_sdk import errors
from coval_agent_sdk import results


def encode_message(text: object) -> str:
    """Encode one agent message as a relay frame after validating it.

    Args:
        text: Nonempty agent reply of at most 64 KiB UTF-8.

    Returns:
        JSON text frame accepted by the relay.

    Raises:
        errors.CovalError: The message is not a nonempty string within limits.
    """
    if not isinstance(text, str) or not text.strip():
        raise errors.CovalError("A text message must be a nonempty string.")
    if len(text.encode()) > _defaults.MAX_TEXT_MESSAGE_BYTES:
        raise errors.CovalError(
            "A text message exceeds the 64 KiB limit; send a shorter reply."
        )
    return json.dumps({"type": "message", "text": text})


class TextState:
    """Private shared transport state; never exported by the public facade."""

    def __init__(self, simulation_id: str) -> None:
        self.simulation_id = simulation_id
        self.websocket: client.ClientConnection | None = None
        self.send_lock = asyncio.Lock()
        self.input: collections.deque[str] = collections.deque()
        self.input_changed = asyncio.Event()
        self.receiving = False
        self.ready = asyncio.Event()
        self.closed = asyncio.Event()
        self.error: BaseException | None = None
        self.completed = False
        self.received_messages = 0
        self.sent_messages = 0
        self.result: results.ConversationResult | None = None

    def feed_message(self, control: dict[str, object]) -> None:
        """Bound unread messages without blocking terminal control frames."""
        text = control.get("text")
        if not isinstance(text, str):
            raise errors.CovalError("Relay sent a message without text.")
        if len(self.input) >= _defaults.MAX_UNREAD_TEXT_MESSAGES:
            raise errors.CovalError(
                "More than 64 unread messages accumulated; consume "
                "receive_messages() promptly."
            )
        self.received_messages += 1
        self.input.append(text)
        self.input_changed.set()

    def finish(self, error: BaseException | None = None) -> None:
        """Wake readers and writers without manufacturing success."""
        if self.error is None:
            self.error = error
        self.closed.set()
        self.input_changed.set()
