"""Keep failed trace cleanup isolated from overlapping conversations."""

from unittest import mock

from opentelemetry import trace
from opentelemetry.sdk import trace as sdk_trace
from opentelemetry.sdk.trace import export as trace_export
import pytest

from coval_agent_sdk import errors
from coval_agent_sdk import tracing


def conversation_span(trace_id, simulation_id, start_time):
    return sdk_trace.ReadableSpan(
        name="conversation",
        context=trace.SpanContext(
            trace_id=trace_id, span_id=trace_id, is_remote=False
        ),
        attributes={"conversation.id": simulation_id},
        start_time=start_time,
        end_time=start_time + 10,
    )


@pytest.mark.parametrize("healthy_start", [5, 10, 15])
def test_failed_cleanup_preserves_an_overlapping_trace(healthy_start):
    exporter = tracing.CovalTraceExporter()
    exporter.export([conversation_span(1, "failed", 10)])
    exporter.discard(simulation_id="failed")

    healthy = conversation_span(2, "healthy", healthy_start)
    assert exporter.export([healthy]) == trace_export.SpanExportResult.SUCCESS
    with mock.patch("coval_agent_sdk.tracing._upload_spans") as upload:
        assert (
            exporter.upload(
                simulation_id="healthy",
                api_base_url="https://api.coval.dev/v1",
                api_key="test-key",
            )
            == 1
        )
    assert upload.call_args.args[0] == (healthy,)


def test_failed_cleanup_rejects_late_spans_from_that_trace():
    exporter = tracing.CovalTraceExporter()
    exporter.export([conversation_span(1, "failed", 10)])
    exporter.discard(simulation_id="failed")
    assert exporter._buffered_span_count == 0
    assert (
        exporter.export([conversation_span(1, "failed", 30)])
        == trace_export.SpanExportResult.FAILURE
    )
    with (
        mock.patch("coval_agent_sdk.tracing._upload_spans") as upload,
        pytest.raises(errors.CovalTraceError, match="discarded spans"),
    ):
        exporter.upload(
            simulation_id="failed",
            api_base_url="https://api.coval.dev/v1",
            api_key="test-key",
        )
    upload.assert_not_called()


def test_failed_cleanup_retains_only_bounded_rejection_metadata():
    exporter = tracing.CovalTraceExporter(
        max_buffered_spans=1, max_buffered_traces=2
    )
    for trace_id in range(1, 8):
        exporter.export(
            [conversation_span(trace_id, str(trace_id), trace_id * 100)]
        )
        exporter.discard(simulation_id=str(trace_id))
        exporter.discard(simulation_id=str(trace_id))
        assert exporter._buffered_span_count == 0
        assert len(exporter._traces) <= 2
