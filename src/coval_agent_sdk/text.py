"""Framework-independent text connection for one Coval conversation."""

from __future__ import annotations

from collections.abc import AsyncIterator

from websockets import exceptions

from coval_agent_sdk import _text
from coval_agent_sdk import errors
from coval_agent_sdk import results


class TextConnection:
    """Exchange text messages with a simulated user.

    Obtain ready connections through CovalTextClient.connect() or a run's
    on_connection handler. The application owns its agent and tasks; the SDK
    owns this connection. Each message the simulated user sends is one user
    turn; each send_message() call is one complete agent reply. Use one
    receive loop per connection.

    Attributes:
        simulation_id: Hosted conversation identifier for correlation.
    """

    def __init__(self, simulation_id: str) -> None:
        """Initialize local state without opening a connection.

        Args:
            simulation_id: Identifier supplied by the client.
        """
        self._state = _text.TextState(simulation_id)

    @property
    def simulation_id(self) -> str:
        """Hosted conversation identifier for application correlation."""
        return self._state.simulation_id

    @property
    def closed(self) -> bool:
        """Whether the conversation has ended, successfully or otherwise."""
        return self._state.closed.is_set()

    @property
    def result(self) -> results.ConversationResult | None:
        """Completed outcome after context exit, or None before finalization."""
        return self._state.result

    async def receive_messages(self) -> AsyncIterator[str]:
        """Yield simulated-user messages until the conversation ends.

        Reply to each message with send_message(). Only one receive iterator
        may be active. A failed connection raises its original error; it does
        not appear as a successful end of input.

        Yields:
            Simulated-user message text in arrival order.

        Raises:
            RuntimeError: Another receive iterator is already active.
            errors.CovalError: The relay or receive buffer fails.
        """
        if self._state.receiving:
            raise RuntimeError(
                "Only one message receiver may consume a connection."
            )
        self._state.receiving = True
        try:
            while True:
                if self._state.error is not None:
                    raise self._state.error
                if self._state.input:
                    yield self._state.input.popleft()
                elif self.closed:
                    return
                else:
                    self._state.input_changed.clear()
                    await self._state.input_changed.wait()
        finally:
            self._state.receiving = False

    async def send_message(self, text: str) -> bool:
        """Send one complete agent reply.

        Concurrent sends are serialized in call order.

        Args:
            text: Nonempty reply text, at most 64 KiB when UTF-8 encoded.

        Returns:
            True if the message was sent; False if normal conversation
            completion happened first. A message is never replayed.

        Raises:
            errors.CovalError: The text is invalid or the connection fails.
        """
        frame = _text.encode_message(text)
        if self._state.error is not None:
            raise self._state.error
        if self.closed:
            return False
        if self._state.websocket is None or not self._state.ready.is_set():
            raise errors.CovalError("The text connection is not ready.")
        async with self._state.send_lock:
            if self.closed:
                if self._state.error is not None:
                    raise self._state.error
                return False
            try:
                await self._state.websocket.send(frame)
            except exceptions.ConnectionClosed:
                # The reader owns authoritative completion after closure.
                await self.wait_closed()
                return False
            except OSError as exception:
                failure = errors.CovalConnectionError(
                    "The relay message write failed."
                )
                self._state.finish(failure)
                raise failure from exception
            self._state.sent_messages += 1
            return True

    async def wait_closed(self) -> None:
        """Wait for conversation completion, independently of evaluation.

        Raises:
            errors.CovalError: The connection failed instead of completing.
        """
        await self._state.closed.wait()
        if self._state.error is not None:
            raise self._state.error
