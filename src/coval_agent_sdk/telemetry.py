"""Dependency-free contract for optional conversation trace delivery."""

from __future__ import annotations

from typing import Protocol


class TraceExporter(Protocol):
    """Flush, upload, and discard traces without exposing provider types.

    Implementations own their tracing provider and buffers. Flush and upload
    run in worker threads during session cleanup, after application cleanup.
    Discard must release local buffers promptly.
    """

    def flush(self, *, timeout_millis: int) -> bool:
        """Flush ended spans before uploading or discarding a conversation.

        Args:
            timeout_millis: Maximum provider flush duration in milliseconds.

        Returns:
            True when the provider flushed successfully, otherwise False.
        """
        ...

    def upload(
        self,
        *,
        simulation_id: str,
        api_base_url: str,
        api_key: str,
        workspace_id: str | None = None,
    ) -> int:
        """Upload one completed conversation, retaining spans on failure.

        Args:
            simulation_id: Identifier from the audio connection.
            api_base_url: Coval API root including the version path.
            api_key: Organization API credential; never log it.
            workspace_id: Workspace selector, or None for the server default.

        Returns:
            Number of spans uploaded successfully.

        Raises:
            errors.CovalTraceError: Delivery fails. Retry retained buffers
                instead of repeating the voice conversation.
        """
        ...

    def discard(self, *, simulation_id: str) -> None:
        """Release a failed conversation's spans after closure and flush.

        Args:
            simulation_id: Identifier from the audio connection.
        """
        ...
