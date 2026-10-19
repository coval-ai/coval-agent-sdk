"""Verify that traces from separate conversations stay separate."""

import unittest
from unittest import mock

from opentelemetry import trace
from opentelemetry.sdk import trace as sdk_trace
from opentelemetry.sdk.trace import export as trace_export
import pytest

from coval_agent_sdk import errors
from coval_agent_sdk import tracing


def make_span(
    *, trace_id: int, simulation_id: str | None = None, start_time: int = 1
) -> sdk_trace.ReadableSpan:
    """Make an ended span without a global tracer provider."""
    return sdk_trace.ReadableSpan(
        name="conversation" if simulation_id is not None else "stt",
        context=trace.SpanContext(
            trace_id=trace_id, span_id=trace_id, is_remote=False
        ),
        attributes={"conversation.id": simulation_id}
        if simulation_id is not None
        else {},
        start_time=start_time,
        end_time=start_time + 1,
    )


class TraceExporterTest(unittest.TestCase):
    """Check the simulation boundary used by the Coval trace endpoint."""

    def test_upload_sends_only_the_requested_conversation(self) -> None:
        collector = tracing.CovalTraceExporter()
        provider = sdk_trace.TracerProvider()
        provider.add_span_processor(trace_export.SimpleSpanProcessor(collector))
        tracer = provider.get_tracer(__name__)
        for simulation_id in ("simulation-a", "simulation-b"):
            with (
                tracer.start_as_current_span(
                    "conversation",
                    attributes={"conversation.id": simulation_id},
                ),
                tracer.start_as_current_span("stt"),
            ):
                pass

        upload = mock.Mock()
        upload.return_value = None
        with mock.patch(
            "coval_agent_sdk.tracing._upload_spans",
            new=upload,
        ) as factory:
            uploaded_count = collector.upload(
                simulation_id="simulation-a",
                api_base_url="https://api.coval.dev/v1",
                api_key="test-key",
                workspace_id="workspace-a",
            )
            remaining_count = collector.upload(
                simulation_id="simulation-b",
                api_base_url="https://api.coval.dev/v1",
                api_key="test-key",
            )

        self.assertEqual(uploaded_count, 2)
        self.assertEqual(
            factory.call_args_list[0].kwargs["workspace_id"],
            "workspace-a",
        )
        self.assertIsNone(factory.call_args_list[1].kwargs["workspace_id"])
        sent_spans = upload.call_args_list[0].args[0]
        self.assertEqual(
            {span.name for span in sent_spans}, {"conversation", "stt"}
        )
        self.assertEqual(len({span.context.trace_id for span in sent_spans}), 1)
        self.assertEqual(
            factory.call_args_list[0].kwargs["simulation_id"],
            "simulation-a",
        )
        self.assertEqual(remaining_count, 2)
        self.assertEqual(
            factory.call_args.kwargs["simulation_id"],
            "simulation-b",
        )
        second_spans = upload.call_args_list[1].args[0]
        self.assertNotEqual(
            sent_spans[0].context.trace_id, second_spans[0].context.trace_id
        )

    def test_upload_sends_spans_that_arrive_during_the_network_request(
        self,
    ) -> None:
        collector = tracing.CovalTraceExporter(max_buffered_spans=2)
        trace_id = 1
        root = make_span(trace_id=trace_id, simulation_id="simulation-a")
        late_span = make_span(trace_id=trace_id)
        collector.export([root])
        batches = []

        def export_during_upload(spans, **options) -> None:
            batches.append(spans)
            if len(batches) == 1:
                self.assertEqual(
                    collector.export([late_span]),
                    trace_export.SpanExportResult.SUCCESS,
                )
            return

        upload = mock.Mock()
        upload.side_effect = export_during_upload
        with mock.patch(
            "coval_agent_sdk.tracing._upload_spans",
            new=upload,
        ):
            self.assertEqual(
                collector.upload(
                    simulation_id="simulation-a",
                    api_base_url="https://api.coval.dev/v1",
                    api_key="test-key",
                ),
                2,
            )
        self.assertEqual(batches, [(root,), (late_span,)])
        self.assertEqual(collector._buffered_span_count, 0)
        self.assertEqual(collector._traces, {})

    def test_orphan_pressure_recovers_without_resurrecting_incomplete_traces(
        self,
    ) -> None:
        for capacity in ({"max_buffered_traces": 1}, {"max_buffered_spans": 1}):
            with self.subTest(capacity=capacity):
                collector = tracing.CovalTraceExporter(**capacity)
                collector.export([make_span(trace_id=1, start_time=1)])
                self.assertEqual(
                    collector.export(
                        [
                            make_span(
                                trace_id=2,
                                simulation_id="healthy",
                                start_time=3,
                            )
                        ]
                    ),
                    trace_export.SpanExportResult.SUCCESS,
                )
                upload = mock.Mock()
                upload.return_value = None
                with mock.patch(
                    "coval_agent_sdk.tracing._upload_spans",
                    new=upload,
                ):
                    self.assertEqual(
                        collector.upload(
                            simulation_id="healthy",
                            api_base_url="https://api.coval.dev/v1",
                            api_key="test-key",
                        ),
                        1,
                    )
                self.assertEqual(
                    collector.export(
                        [
                            make_span(
                                trace_id=1,
                                simulation_id="abandoned",
                                start_time=1,
                            )
                        ]
                    ),
                    trace_export.SpanExportResult.FAILURE,
                )
                with self.assertRaisesRegex(
                    errors.CovalTraceError, "discarded spans"
                ):
                    collector.upload(
                        simulation_id="abandoned",
                        api_base_url="https://api.coval.dev/v1",
                        api_key="test-key",
                    )

    def test_discard_releases_only_the_failed_conversation(self) -> None:
        collector = tracing.CovalTraceExporter()
        collector.export(
            [
                make_span(trace_id=1, simulation_id="failed"),
                make_span(trace_id=2, simulation_id="healthy"),
            ]
        )
        collector.discard(simulation_id="failed")
        self.assertEqual(collector._buffered_span_count, 1)
        upload = mock.Mock()
        upload.return_value = None
        with mock.patch(
            "coval_agent_sdk.tracing._upload_spans",
            new=upload,
        ):
            self.assertEqual(
                collector.upload(
                    simulation_id="healthy",
                    api_base_url="https://api.coval.dev/v1",
                    api_key="test-key",
                ),
                1,
            )

    def test_exporter_setup_failure_keeps_spans_for_retry(self) -> None:
        collector = tracing.CovalTraceExporter()
        provider = sdk_trace.TracerProvider()
        provider.add_span_processor(trace_export.SimpleSpanProcessor(collector))
        with provider.get_tracer(__name__).start_as_current_span(
            "conversation", attributes={"conversation.id": "simulation-a"}
        ):
            pass

        upload_options = {
            "simulation_id": "simulation-a",
            "api_base_url": "https://api.coval.dev/v1",
            "api_key": "test-key",
        }
        with (
            mock.patch(
                "coval_agent_sdk.tracing._upload_spans",
                side_effect=ValueError("bad URL"),
            ),
            self.assertRaisesRegex(errors.CovalTraceError, "bad URL"),
        ):
            collector.upload(**upload_options)

        upload = mock.Mock()
        upload.return_value = None
        with mock.patch(
            "coval_agent_sdk.tracing._upload_spans",
            new=upload,
        ):
            self.assertEqual(collector.upload(**upload_options), 1)

    def test_overflow_rejects_damaged_trace_and_recovers(
        self,
    ) -> None:
        collector = tracing.CovalTraceExporter(max_buffered_spans=3)
        damaged_trace_id = 1
        unaffected_trace_id = 2
        subsequent_trace_id = 3
        collector.export(
            [
                make_span(trace_id=damaged_trace_id),
                make_span(trace_id=damaged_trace_id),
                make_span(
                    trace_id=unaffected_trace_id, simulation_id="unaffected"
                ),
            ]
        )
        self.assertEqual(
            collector.export(
                [make_span(trace_id=damaged_trace_id, simulation_id="damaged")]
            ),
            trace_export.SpanExportResult.FAILURE,
        )
        upload_options = {
            "api_base_url": "https://api.coval.dev/v1",
            "api_key": "test-key",
        }
        with self.assertRaisesRegex(
            errors.CovalTraceError,
            "discarded spans for simulation damaged",
        ):
            collector.upload(simulation_id="damaged", **upload_options)

        upload = mock.Mock()
        upload.return_value = None
        with mock.patch(
            "coval_agent_sdk.tracing._upload_spans",
            new=upload,
        ):
            self.assertEqual(
                collector.upload(simulation_id="unaffected", **upload_options),
                1,
            )
            self.assertEqual(
                collector.export(
                    [
                        make_span(
                            trace_id=subsequent_trace_id,
                            simulation_id="subsequent",
                        )
                    ]
                ),
                trace_export.SpanExportResult.SUCCESS,
            )
            self.assertEqual(
                collector.upload(simulation_id="subsequent", **upload_options),
                1,
            )
        self.assertEqual(upload.call_count, 2)

    def test_failed_upload_keeps_its_capacity_reserved_during_concurrent_export(
        self,
    ) -> None:
        collector = tracing.CovalTraceExporter(max_buffered_spans=3)
        uploading_trace_id = 1
        concurrent_trace_id = 2
        collector.export(
            [make_span(trace_id=uploading_trace_id, simulation_id="uploading")]
        )

        def export_during_upload(spans, **options) -> None:
            self.assertEqual(len(spans), 1)
            self.assertEqual(
                collector.export(
                    [
                        make_span(trace_id=concurrent_trace_id),
                        make_span(trace_id=concurrent_trace_id),
                        make_span(
                            trace_id=concurrent_trace_id,
                            simulation_id="concurrent",
                        ),
                    ]
                ),
                trace_export.SpanExportResult.FAILURE,
            )
            raise errors.CovalTraceError(
                "Coval could not receive traces for simulation uploading"
            )

        upload_options = {
            "api_base_url": "https://api.coval.dev/v1",
            "api_key": "test-key",
        }
        upload = mock.Mock()
        upload.side_effect = export_during_upload
        with mock.patch(
            "coval_agent_sdk.tracing._upload_spans",
            new=upload,
        ):
            with self.assertRaisesRegex(
                errors.CovalTraceError,
                ("could not receive traces for simulation uploading"),
            ):
                collector.upload(simulation_id="uploading", **upload_options)
            with self.assertRaisesRegex(
                errors.CovalTraceError,
                "discarded spans for simulation concurrent",
            ):
                collector.upload(simulation_id="concurrent", **upload_options)
            upload.side_effect = None
            upload.return_value = None
            self.assertEqual(
                collector.upload(simulation_id="uploading", **upload_options), 1
            )

    def test_saturation_cannot_resurrect_a_damaged_trace(
        self,
    ) -> None:
        collector = tracing.CovalTraceExporter(
            max_buffered_spans=3, max_buffered_traces=1
        )
        retained_trace_id = 1
        rejected_trace_id = 2
        subsequent_trace_id = 3
        collector.export(
            [make_span(trace_id=retained_trace_id, simulation_id="retained")]
        )
        with mock.patch(
            "coval_agent_sdk.tracing.time.time_ns", return_value=10
        ):
            self.assertEqual(
                collector.export([make_span(trace_id=rejected_trace_id)]),
                trace_export.SpanExportResult.FAILURE,
            )

        upload_options = {
            "api_base_url": "https://api.coval.dev/v1",
            "api_key": "test-key",
        }
        upload = mock.Mock()
        upload.return_value = None
        with mock.patch(
            "coval_agent_sdk.tracing._upload_spans",
            new=upload,
        ):
            self.assertEqual(
                collector.upload(simulation_id="retained", **upload_options), 1
            )
            self.assertEqual(
                collector.export(
                    [
                        make_span(
                            trace_id=rejected_trace_id, simulation_id="rejected"
                        )
                    ]
                ),
                trace_export.SpanExportResult.FAILURE,
            )
            with self.assertRaisesRegex(
                errors.CovalTraceError,
                "discarded spans for simulation rejected",
            ):
                collector.upload(simulation_id="rejected", **upload_options)
            self.assertEqual(
                collector.export(
                    [
                        make_span(
                            trace_id=subsequent_trace_id,
                            simulation_id="subsequent",
                            start_time=11,
                        )
                    ]
                ),
                trace_export.SpanExportResult.SUCCESS,
            )
            self.assertEqual(
                collector.upload(simulation_id="subsequent", **upload_options),
                1,
            )

    def test_damaged_trace_metadata_does_not_consume_the_span_budget(
        self,
    ) -> None:
        collector = tracing.CovalTraceExporter(
            max_buffered_spans=1, max_buffered_traces=2
        )
        collector.export([make_span(trace_id=1)])
        self.assertEqual(
            collector.export([make_span(trace_id=1)]),
            trace_export.SpanExportResult.FAILURE,
        )
        self.assertEqual(
            collector.export(
                [make_span(trace_id=2, simulation_id="unaffected")]
            ),
            trace_export.SpanExportResult.SUCCESS,
        )
        upload = mock.Mock()
        upload.return_value = None
        with mock.patch(
            "coval_agent_sdk.tracing._upload_spans",
            new=upload,
        ):
            self.assertEqual(
                collector.upload(
                    simulation_id="unaffected",
                    api_base_url="https://api.coval.dev/v1",
                    api_key="test-key",
                ),
                1,
            )


def test_export_rejects_a_span_without_a_trace_context():
    collector = tracing.CovalTraceExporter()
    assert (
        collector.export([sdk_trace.ReadableSpan(name="missing-context")])
        == trace_export.SpanExportResult.FAILURE
    )


if __name__ == "__main__":
    unittest.main()


@pytest.mark.parametrize("outcome", [True, False, "missing", "error"])
def test_exporter_flush_delegates_to_the_configured_provider(
    monkeypatch, outcome
):
    exporter = tracing.CovalTraceExporter()
    provider = object() if outcome == "missing" else mock.Mock()
    if outcome != "missing":
        provider.force_flush.return_value = outcome
    if outcome == "error":
        provider.force_flush.side_effect = RuntimeError("provider flush failed")
    monkeypatch.setattr(tracing.trace, "get_tracer_provider", lambda: provider)
    if outcome == "error":
        with pytest.raises(RuntimeError, match="provider flush failed"):
            exporter.flush(timeout_millis=777)
    else:
        assert exporter.flush(timeout_millis=777) is (
            False if outcome == "missing" else outcome
        )
    if outcome != "missing":
        provider.force_flush.assert_called_once_with(timeout_millis=777)
