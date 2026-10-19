"""Buffer Pipecat spans and upload one completed conversation to Coval."""

from collections.abc import Sequence
import contextlib
import dataclasses
import threading
import time

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.sdk.trace.export import SpanExportResult

DEFAULT_MAX_BUFFERED_SPANS = 10_000
DEFAULT_MAX_BUFFERED_TRACES = 10_000
CONVERSATION_ID_ATTRIBUTE = "conversation.id"


class CovalTraceError(RuntimeError):
  """A conversation trace could not be prepared or uploaded."""


@dataclasses.dataclass
class _TraceBuffer:
  """Keep bounded spans and the correlation needed to reject an incomplete trace."""

  spans: list[ReadableSpan] = dataclasses.field(default_factory=list)
  simulation_id: str | None = None
  damaged: bool = False
  uploading: bool = False
  rejected_before_epoch_nanoseconds: int = 0
  latest_span_epoch_nanoseconds: int = 0


class CovalTraceExporter(SpanExporter):
  """Collect process-wide spans and upload each Pipecat trace separately."""

  def __init__(
    self,
    *,
    max_buffered_spans: int = DEFAULT_MAX_BUFFERED_SPANS,
    max_buffered_traces: int = DEFAULT_MAX_BUFFERED_TRACES,
  ) -> None:
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

  def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
    """Buffer ended spans until their conversation root identifies the run."""
    result = SpanExportResult.SUCCESS
    with self._lock:
      for span in spans:
        context = span.context
        if context is None:
          result = SpanExportResult.FAILURE
          continue
        trace_id = context.trace_id
        buffer = self._traces.get(trace_id)
        if buffer is None:
          if len(self._traces) >= self._max_buffered_traces:
            self._discard_oldest_orphan()
          if len(self._traces) >= self._max_buffered_traces:
            # A later root must not make a trace with discarded spans look complete.
            self._rejected_before_epoch_nanoseconds = max(self._rejected_before_epoch_nanoseconds, time.time_ns())
            result = SpanExportResult.FAILURE
            continue
          buffer = _TraceBuffer(rejected_before_epoch_nanoseconds=self._rejected_before_epoch_nanoseconds)
          self._traces[trace_id] = buffer
        buffer.latest_span_epoch_nanoseconds = max(
          buffer.latest_span_epoch_nanoseconds, span.end_time or span.start_time or time.time_ns()
        )
        if span.name == "conversation":
          simulation_id = (span.attributes or {}).get(CONVERSATION_ID_ATTRIBUTE)
          if isinstance(simulation_id, str):
            buffer.simulation_id = simulation_id
        if buffer.rejected_before_epoch_nanoseconds and (
          span.start_time is None or span.start_time <= buffer.rejected_before_epoch_nanoseconds
        ):
          buffer.damaged = True
        if not buffer.damaged and self._buffered_span_count >= self._max_buffered_spans:
          self._discard_oldest_orphan(exclude_trace_id=trace_id)
          if self._buffered_span_count >= self._max_buffered_spans:
            buffer.damaged = True
        if buffer.damaged:
          if buffer.uploading:
            # The network call still owns these spans until it finishes.
            result = SpanExportResult.FAILURE
            continue
          self._buffered_span_count -= len(buffer.spans)
          buffer.spans.clear()
          result = SpanExportResult.FAILURE
          continue
        buffer.spans.append(span)
        self._buffered_span_count += 1
    return result

  def _discard_oldest_orphan(self, *, exclude_trace_id: int | None = None) -> None:
    """Reclaim abandoned metadata under the lock without evicting retryable uploads."""
    for trace_id, buffer in self._traces.items():
      if trace_id == exclude_trace_id:
        continue
      if buffer.uploading or (buffer.simulation_id is not None and not buffer.damaged):
        continue
      self._buffered_span_count -= len(buffer.spans)
      self._rejected_before_epoch_nanoseconds = max(
        self._rejected_before_epoch_nanoseconds, buffer.latest_span_epoch_nanoseconds
      )
      del self._traces[trace_id]
      return

  def discard(self, *, simulation_id: str) -> None:
    """Release a failed conversation after its agent has closed and spans have flushed."""
    with self._lock:
      trace_ids = [
        trace_id
        for trace_id, buffer in self._traces.items()
        if buffer.simulation_id == simulation_id and not buffer.uploading
      ]
      for trace_id in trace_ids:
        buffer = self._traces.pop(trace_id)
        self._buffered_span_count -= len(buffer.spans)
        self._rejected_before_epoch_nanoseconds = max(
          self._rejected_before_epoch_nanoseconds, buffer.latest_span_epoch_nanoseconds
        )

  def upload(self, *, simulation_id: str, api_base_url: str, api_key: str, workspace_id: str | None = None) -> int:
    """Post only spans belonging to this completed Pipecat conversation."""
    with self._lock:
      matching_trace_ids = [
        trace_id for trace_id, buffer in self._traces.items() if buffer.simulation_id == simulation_id
      ]
      if len(matching_trace_ids) != 1:
        raise CovalTraceError(
          f"Expected one completed Pipecat trace for simulation {simulation_id}, "
          f"found {len(matching_trace_ids)}. Set PipelineWorker conversation_id to the simulation ID."
        )
      trace_id = matching_trace_ids[0]
      buffer = self._traces[trace_id]
      if buffer.uploading:
        raise CovalTraceError(f"A Coval trace upload is already in progress for simulation {simulation_id}.")
      if buffer.damaged:
        del self._traces[trace_id]
        self._buffered_span_count -= len(buffer.spans)
        raise CovalTraceError(
          f"The Coval trace buffer discarded spans for simulation {simulation_id}; "
          "increase max_buffered_spans before starting another conversation."
        )
      buffer.uploading = True
      # Keep these spans counted while the network call runs so a retry stays bounded.
      spans = tuple(buffer.spans)

    exporter: OTLPSpanExporter | None = None
    uploaded_count = 0
    try:
      headers = {
        "X-API-Key": api_key,
        "X-Simulation-Id": simulation_id,
      }
      if workspace_id is not None:
        headers["X-Coval-Workspace-Id"] = workspace_id
      exporter = OTLPSpanExporter(
        endpoint=f"{api_base_url.rstrip('/')}/traces",
        headers=headers,
      )
      while spans:
        result = exporter.export(spans)
        if result != SpanExportResult.SUCCESS:
          raise CovalTraceError(f"Coval rejected or could not receive traces for simulation {simulation_id}.")
        uploaded_count += len(spans)
        spans = self._finish_upload(trace_id, succeeded=True, uploaded_span_count=len(spans))
    except Exception as exception:
      self._finish_upload(trace_id, succeeded=False)
      raise CovalTraceError(f"Coval trace upload failed for simulation {simulation_id}: {exception}") from exception
    finally:
      if exporter is not None:
        with contextlib.suppress(Exception):
          exporter.shutdown()
    return uploaded_count

  def _finish_upload(self, trace_id: int, *, succeeded: bool, uploaded_span_count: int = 0) -> tuple[ReadableSpan, ...]:
    """Release only sent spans and return late arrivals for the same upload."""
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
          raise CovalTraceError(
            f"The Coval trace buffer discarded spans during upload for simulation {buffer.simulation_id}."
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
