"""Exceptions raised by the Coval voice API and relay."""

from __future__ import annotations

from coval_agent_sdk import results


class TunnelError(RuntimeError):
    """The Coval API or relay could not complete a voice session."""


class CovalHttpError(TunnelError):
    """An HTTP error returned by the Coval API.

    Attributes:
        status_code: HTTP status returned by the server.
    """

    def __init__(self, *, status_code: int, message: str) -> None:
        """Initialize an HTTP failure.

        Args:
            status_code: HTTP status returned by the server.
            message: Diagnostic message without authentication credentials.
        """
        super().__init__(message)
        self.status_code = status_code


class CovalConnectionError(TunnelError):
    """An API request could not reach Coval."""


class RunError(TunnelError):
    """A launched run failed, retaining its known identity and local outcomes.

    Attributes:
        result: Snapshot after local cleanup. Hosted work may still be running.
    """

    def __init__(self, message: str, *, result: results.RunResult) -> None:
        """Attach the partial run outcome to a failure.

        Args:
            message: Explanation of the failure.
            result: Last known hosted status and completed local sessions.
        """
        super().__init__(message)
        self.result = result


class CovalTraceError(RuntimeError):
    """A conversation trace could not be prepared or uploaded."""
