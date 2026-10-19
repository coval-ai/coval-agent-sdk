"""Exceptions raised by the Coval voice API and relay."""

from __future__ import annotations


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
