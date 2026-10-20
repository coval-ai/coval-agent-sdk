"""Buffer spans and upload one completed conversation to Coval."""

from __future__ import annotations

from collections.abc import Sequence
import dataclasses
import threading
import time

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.common import trace_encoder
from opentelemetry.sdk import trace as sdk_trace
from opentelemetry.sdk.trace import export as trace_export

from coval_agent_sdk import _http
from coval_agent_sdk import errors

DEFAULT_MAX_BUFFERED_SPANS = 10_000
DEFAULT_MAX_BUFFERED_TRACES = 10_000
CONVERSATION_ID_ATTRIBUTE = "conversation.id"


@dataclasses.dataclass
class _TraceBuffer:
    """Retain spans and correlation to reject incomplete traces."""

    spans: list[sdk_trace.ReadableSpan] = dataclasses.field(
        default_factory=list[sdk_trace.ReadableSpan]
    )
    simulation_id: str | None = None
    damaged: bool = False
    uploading: bool = False
    rejected_before_epoch_nanoseconds: int = 0
    latest_span_epoch_nanoseconds: int = 0


def _upload_spans(
    spans: Sequence[sdk_trace.ReadableSpan],
    *,
    simulation_id: str,
    api_base_url: str,
    api_key: str,
    workspace_id: str | None,
) -> None:
    """Encode OTLP and send it through the private, silent HTTP transport."""
    api = _http.CovalApi(
        api_base_url=api_base_url,
        api_key=api_key,
        workspace_id=workspace_id,
    )
    payload = trace_encoder.encode_spans(spans).SerializeToString()
    api.post_protobuf(
        "traces", payload, headers={"X-Simulation-Id": simulation_id}
    )


class CovalTraceExporter(trace_export.SpanExporter):
    """Collect process-wide spans and upload each conversation separately."""

    def __init__(
        self,
        *,
        max_buffered_spans: int = DEFAULT_MAX_BUFFERED_SPANS,
        max_buffered_traces: int = DEFAULT_MAX_BUFFERED_TRACES,
    ) -> None:
        """Configure bounded, process-wide buffering of ended spans.

        Args:
            max_buffered_spans: Positive limit on all retained ended spans.
            max_buffered_traces: Positive limit on trace metadata, including
                damaged traces retained to reject incomplete uploads.

        Raises:
            ValueError: Either buffer limit is not positive.
        """
        if max_buffered_spans < 1:
            raise ValueError("max_buffered_spans must be positive.")
        if max_buffered_traces < 1:
            raise ValueError("max_buffered_traces must be positive.")
        self._lock = threading.Lock()
        self._traces: dict[int, _TraceBuffer] = {}
        self._buffered_span_count = 0
        self._max_buffered_spans = max_buffered_spans
        self._max_buffered_traces = max_buffered_traces
        self._rejected_before_epoch_nanoseconds = 0

    def flush(self, *, timeout_millis: int) -> bool:
        """Flush ended provider spans before conversation finalization.

        Args:
            timeout_millis: Maximum provider flush duration in milliseconds.

        Returns:
            True when the configured provider flushed successfully. False when
            no flush-capable provider is configured or it reports failure.

        Raises:
            Exception: The configured provider raises while flushing.
        """
        provider = trace.get_tracer_provider()
        flush = getattr(provider, "force_flush", None)
        return bool(callable(flush) and flush(timeout_millis=timeout_millis))

    def export(
        self, spans: Sequence[sdk_trace.ReadableSpan]
    ) -> trace_export.SpanExportResult:
        """Buffer ended spans until their conversation root identifies the run.

        Args:
            spans: Ended OpenTelemetry spans, possibly from multiple
                conversations.

        Returns:
            SUCCESS if every span is buffered, otherwise FAILURE. Damaged traces
            are never uploaded as complete conversations.
        """
        result = trace_export.SpanExportResult.SUCCESS
        with self._lock:
            for span in spans:
                context = span.context
                if context is None:
                    result = trace_export.SpanExportResult.FAILURE
                    continue
                trace_id = context.trace_id
                buffer = self._traces.get(trace_id)
                if buffer is None:
                    if len(self._traces) >= self._max_buffered_traces:
                        self._discard_oldest_orphan()
                    if len(self._traces) >= self._max_buffered_traces:
                        # A later root must not make a trace with discarded
                        # spans look complete.
                        self._rejected_before_epoch_nanoseconds = max(
                            self._rejected_before_epoch_nanoseconds,
                            time.time_ns(),
                        )
                        result = trace_export.SpanExportResult.FAILURE
                        continue
                    buffer = _TraceBuffer(
                        rejected_before_epoch_nanoseconds=self._rejected_before_epoch_nanoseconds
                    )
                    self._traces[trace_id] = buffer
                buffer.latest_span_epoch_nanoseconds = max(
                    buffer.latest_span_epoch_nanoseconds,
                    span.end_time or span.start_time or time.time_ns(),
                )
                if span.name == "conversation":
                    simulation_id = (span.attributes or {}).get(
                        CONVERSATION_ID_ATTRIBUTE
                    )
                    if isinstance(simulation_id, str):
                        buffer.simulation_id = simulation_id
                if buffer.rejected_before_epoch_nanoseconds and (
                    span.start_time is None
                    or span.start_time
                    <= buffer.rejected_before_epoch_nanoseconds
                ):
                    buffer.damaged = True
                if (
                    not buffer.damaged
                    and self._buffered_span_count >= self._max_buffered_spans
                ):
                    self._discard_oldest_orphan(exclude_trace_id=trace_id)
                    if self._buffered_span_count >= self._max_buffered_spans:
                        buffer.damaged = True
                if buffer.damaged:
                    if buffer.uploading:
                        # The network call still owns these spans until it
                        # finishes.
                        result = trace_export.SpanExportResult.FAILURE
                        continue
                    self._buffered_span_count -= len(buffer.spans)
                    buffer.spans.clear()
                    result = trace_export.SpanExportResult.FAILURE
                    continue
                buffer.spans.append(span)
                self._buffered_span_count += 1
        return result

    def _discard_oldest_orphan(
        self, *, exclude_trace_id: int | None = None
    ) -> None:
        """Reclaim abandoned metadata under the lock, retaining uploads."""
        for trace_id, buffer in self._traces.items():
            if trace_id == exclude_trace_id:
                continue
            if buffer.uploading or (
                buffer.simulation_id is not None and not buffer.damaged
            ):
                continue
            self._buffered_span_count -= len(buffer.spans)
            self._rejected_before_epoch_nanoseconds = max(
                self._rejected_before_epoch_nanoseconds,
                buffer.latest_span_epoch_nanoseconds,
            )
            del self._traces[trace_id]
            return

    def discard(self, *, simulation_id: str) -> None:
        """Discard failed traces after application cleanup and span flush.

        Args:
            simulation_id: Conversation identifier from the audio connection.
        """
        with self._lock:
            trace_ids = [
                trace_id
                for trace_id, buffer in self._traces.items()
                if buffer.simulation_id == simulation_id
                and not buffer.uploading
            ]
            for trace_id in trace_ids:
                buffer = self._traces.pop(trace_id)
                self._buffered_span_count -= len(buffer.spans)
                self._rejected_before_epoch_nanoseconds = max(
                    self._rejected_before_epoch_nanoseconds,
                    buffer.latest_span_epoch_nanoseconds,
                )

    def upload(
        self,
        *,
        simulation_id: str,
        api_base_url: str,
        api_key: str,
        workspace_id: str | None = None,
    ) -> int:
        """Upload only the completed conversation's spans, retaining failures.

        Call after application cleanup and its spans have been flushed. This is
        a blocking operation; async callers must run it in a worker thread. A
        failed upload can be retried without rerunning the voice conversation.

        Args:
            simulation_id: Conversation identifier from the audio connection.
            api_base_url: API root including the version path.
            api_key: Organization API key for the target workspace.
            workspace_id: Workspace selector, or None for the server default.

        Returns:
            Number of spans successfully uploaded.

        Raises:
            errors.CovalTraceError: The trace is missing, ambiguous, damaged,
                already being uploaded, or rejected by the trace endpoint.
        """
        with self._lock:
            matching_trace_ids = [
                trace_id
                for trace_id, buffer in self._traces.items()
                if buffer.simulation_id == simulation_id
            ]
            if len(matching_trace_ids) != 1:
                raise errors.CovalTraceError(
                    f"Expected one completed conversation trace "
                    f"for simulation {simulation_id}, found "
                    f"{len(matching_trace_ids)}. Set the conversation "
                    f"root span's conversation.id to the simulation ID."
                )
            trace_id = matching_trace_ids[0]
            buffer = self._traces[trace_id]
            if buffer.uploading:
                raise errors.CovalTraceError(
                    f"A Coval trace upload is already in "
                    f"progress for simulation "
                    f"{simulation_id}."
                )
            if buffer.damaged:
                del self._traces[trace_id]
                self._buffered_span_count -= len(buffer.spans)
                raise errors.CovalTraceError(
                    f"The Coval trace buffer discarded spans "
                    f"for simulation {simulation_id}; "
                    f"increase max_buffered_spans before "
                    f"starting another conversation."
                )
            buffer.uploading = True
            # Keep these spans counted while the network call runs so a retry
            # stays bounded.
            spans = tuple(buffer.spans)

        uploaded_count = 0
        try:
            while spans:
                _upload_spans(
                    spans,
                    simulation_id=simulation_id,
                    api_base_url=api_base_url,
                    api_key=api_key,
                    workspace_id=workspace_id,
                )
                uploaded_count += len(spans)
                spans = self._finish_upload(
                    trace_id, succeeded=True, uploaded_span_count=len(spans)
                )
        except Exception as exception:
            self._finish_upload(trace_id, succeeded=False)
            raise errors.CovalTraceError(
                f"Coval trace upload failed for "
                f"simulation {simulation_id}: "
                f"{exception}"
            ) from exception
        return uploaded_count

    def _finish_upload(
        self, trace_id: int, *, succeeded: bool, uploaded_span_count: int = 0
    ) -> tuple[sdk_trace.ReadableSpan, ...]:
        """Release sent spans and return late arrivals for this upload."""
        with self._lock:
            buffer = self._traces.get(trace_id)
            if buffer is None:
                return ()
            if succeeded:
                del buffer.spans[:uploaded_span_count]
                self._buffered_span_count -= uploaded_span_count
                if buffer.damaged:
                    buffer.uploading = False
                    self._buffered_span_count -= len(buffer.spans)
                    buffer.spans.clear()
                    raise errors.CovalTraceError(
                        f"The Coval trace buffer discarded spans "
                        f"during upload for simulation "
                        f"{buffer.simulation_id}."
                    )
                if buffer.spans:
                    return tuple(buffer.spans)
                del self._traces[trace_id]
            else:
                buffer.uploading = False
                if buffer.damaged:
                    self._buffered_span_count -= len(buffer.spans)
                    buffer.spans.clear()
            return ()

    def shutdown(self) -> None:
        """Release buffered spans when the application shuts down."""
        with self._lock:
            self._traces.clear()
            self._buffered_span_count = 0
            self._rejected_before_epoch_nanoseconds = 0
