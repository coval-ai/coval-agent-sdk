"""Run and connection outcomes, application ownership, and silent failures."""

import asyncio
import dataclasses
import json
import types
from unittest import mock

import pytest

import coval_agent_sdk
from coval_agent_sdk import _connection
from coval_agent_sdk import _defaults
from coval_agent_sdk import _runs
from coval_agent_sdk import errors

READY = json.dumps(
    {
        "type": "session_ready",
        "audio": {
            "encoding": "pcm_s16le",
            "sample_rate_hz": 16000,
            "channels": 1,
        },
    }
)
PCM = b"\x01\x00" * 320
OUTPUT = b"\x02\x00" * 320


class Socket:
    def __init__(self):
        self.sent = asyncio.Event()
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def __aiter__(self):
        yield READY
        yield PCM
        await self.sent.wait()
        yield '{"type":"session_end"}'

    async def send(self, value):
        if isinstance(value, bytes):
            assert value == OUTPUT
            self.sent.set()


async def handle(connection):
    async for audio in connection.receive_audio():
        assert audio == PCM
        await connection.send_audio(OUTPUT)


def setup_run(
    monkeypatch, client, hosted_status="COMPLETED", ids=("conversation-a",)
):
    sockets = {}

    async def connect(api, simulation_id):
        socket = sockets[simulation_id] = Socket()
        return socket

    async def read(path, *, query=None):
        await asyncio.sleep(0)
        if path == "simulations":
            return {
                "simulations": [
                    {"simulation_id": name, "status": "RUNNING"} for name in ids
                ]
            }
        return {"run": {"status": hosted_status}}

    monkeypatch.setattr(
        client._api,
        "post",
        mock.AsyncMock(return_value={"run": {"run_id": "run-a"}}),
    )
    monkeypatch.setattr(client._api, "get", read)
    monkeypatch.setattr(_connection, "open_voice_connection", connect)
    monkeypatch.setattr(_defaults, "POLL_INTERVAL_SECONDS", 0)
    return sockets


@pytest.mark.asyncio
@pytest.mark.parametrize("queued_status", ["PENDING", "IN QUEUE"])
async def test_run_defers_queued_connections_within_batch_deadline(
    monkeypatch, queued_status
):
    clock, calls, connected = [0.0], [0], []
    client = coval_agent_sdk.CovalVoiceClient(api_key="synthetic-key")
    sockets = setup_run(monkeypatch, client)
    original = _connection.open_voice_connection

    async def connect(api, simulation_id):
        connected.append(clock[0])
        return await original(api, simulation_id)

    async def read(path, *, query=None):
        await asyncio.sleep(0)
        if path == "simulations":
            calls[0] += 1
            if calls[0] == 2:
                assert connected == []
                clock[0] = _defaults.CONNECT_WAIT_SECONDS + 1
            return {
                "simulations": [
                    {
                        "simulation_id": "conversation-a",
                        "status": queued_status
                        if calls[0] == 1
                        else "IN PROGRESS",
                    }
                ]
            }
        return {
            "run": {"status": "IN PROGRESS" if calls[0] == 1 else "COMPLETED"}
        }

    monkeypatch.setattr(_connection, "open_voice_connection", connect)
    monkeypatch.setattr(client._api, "get", read)
    monkeypatch.setattr(
        _runs, "time", types.SimpleNamespace(monotonic=lambda: clock[0])
    )
    result = await asyncio.wait_for(
        client.run(
            coval_agent_sdk.RunConfig(
                "agent-a",
                "persona-a",
                "test-set-a",
                run_timeout_seconds=_defaults.CONNECT_WAIT_SECONDS * 2,
            ),
            handle,
        ),
        2,
    )
    assert connected == [_defaults.CONNECT_WAIT_SECONDS + 1]
    assert result == coval_agent_sdk.RunResult(
        "run-a",
        "COMPLETED",
        (coval_agent_sdk.ConversationResult("conversation-a", 640, 640),),
    )
    assert sockets["conversation-a"].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("hosted_status", ["COMPLETED", "FAILED"])
async def test_run_delivers_distinct_connections_and_structured_silent_results(
    monkeypatch, capsys, caplog, hosted_status
):
    caplog.set_level("DEBUG")
    client = coval_agent_sdk.CovalVoiceClient(api_key="synthetic-key")
    sockets = setup_run(
        monkeypatch, client, hosted_status, ("conversation-a", "conversation-b")
    )
    connections = []
    cleaned = []

    async def application(connection):
        connections.append(connection)
        try:
            await handle(connection)
        finally:
            cleaned.append(connection.simulation_id)

    operation = client.run(
        coval_agent_sdk.RunConfig(
            "agent-a", "persona-a", "test-set-a", concurrency=2
        ),
        on_connection=application,
    )
    if hosted_status == "FAILED":
        with pytest.raises(coval_agent_sdk.RunError) as raised:
            await asyncio.wait_for(operation, 2)
        result = raised.value.result
    else:
        result = await asyncio.wait_for(operation, 2)
    assert result == coval_agent_sdk.RunResult(
        "run-a",
        hosted_status,
        (
            coval_agent_sdk.ConversationResult("conversation-a", 640, 640),
            coval_agent_sdk.ConversationResult("conversation-b", 640, 640),
        ),
    )
    assert {c.simulation_id for c in connections} == {
        "conversation-a",
        "conversation-b",
    }
    assert connections[0] is not connections[1]
    assert set(cleaned) == set(sockets)
    assert all(socket.closed for socket in sockets.values())
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.run_id = "changed"
    assert capsys.readouterr() == ("", "")
    assert caplog.records == []


@pytest.mark.asyncio
@pytest.mark.parametrize("trace_failure", [None, "flush", "upload"])
async def test_application_cleanup_precedes_final_result_and_trace_delivery(
    monkeypatch, capsys, caplog, trace_failure
):
    caplog.set_level("DEBUG")
    client = coval_agent_sdk.CovalVoiceClient(
        api_key="synthetic-key", workspace_id="workspace-a"
    )
    sockets = setup_run(monkeypatch, client)
    order = []
    exporter = mock.Mock()

    def flush(**kwargs):
        assert order == ["application-cleaned"]
        assert sockets["conversation-a"].closed
        order.append("flush")
        return trace_failure != "flush"

    exporter.flush.side_effect = flush
    exporter.upload.return_value = 7
    if trace_failure == "upload":
        exporter.upload.side_effect = errors.CovalTraceError("upload rejected")
    client.trace_exporter = exporter
    async with client.connect("conversation-a") as connection:
        await handle(connection)
        assert connection.result is None
        order.append("application-cleaned")
    result = connection.result
    assert result.simulation_id == "conversation-a"
    assert (result.received_audio_bytes, result.sent_audio_bytes) == (640, 640)
    if trace_failure:
        assert result.trace.uploaded_spans is None
        assert (
            "flush" if trace_failure == "flush" else "upload rejected"
        ) in result.trace.error
    else:
        assert result.trace == coval_agent_sdk.TraceResult(uploaded_spans=7)
    if trace_failure != "flush":
        exporter.upload.assert_called_once_with(
            simulation_id="conversation-a",
            api_base_url="https://api.coval.dev/v1",
            api_key="synthetic-key",
            workspace_id="workspace-a",
        )
    exporter.discard.assert_not_called()
    assert capsys.readouterr() == ("", "")
    assert caplog.records == []


@pytest.mark.asyncio
@pytest.mark.parametrize("after_launch", [False, True])
async def test_sdk_failure_keeps_launch_identity_and_last_observed_status(
    monkeypatch, after_launch
):
    client = coval_agent_sdk.CovalVoiceClient(api_key="synthetic-key")
    original = coval_agent_sdk.CovalHttpError(status_code=403, message="denied")
    monkeypatch.setattr(
        client._api,
        "post",
        mock.AsyncMock(
            return_value={"run": {"run_id": "run-a"}},
            side_effect=None if after_launch else original,
        ),
    )
    monkeypatch.setattr(
        client._api, "get", mock.AsyncMock(side_effect=original)
    )
    with pytest.raises(coval_agent_sdk.CovalError) as raised:
        await client.run(
            coval_agent_sdk.RunConfig("agent-a", "persona-a", "test-set-a"),
            handle,
        )
    if after_launch:
        assert isinstance(raised.value, coval_agent_sdk.RunError)
        assert raised.value.result == coval_agent_sdk.RunResult(
            "run-a", None, ()
        )
        assert raised.value.__cause__ is original
    else:
        assert raised.value is original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry", "status"),
    [("connect", "RUNNING"), ("run", "RUNNING"), ("run", "COMPLETED")],
)
@pytest.mark.parametrize("phase", ["start", "input", "output", "cleanup"])
async def test_application_errors_keep_identity_and_cleanup_owned_resources(
    monkeypatch, entry, status, phase
):
    original = RuntimeError(f"application {phase} failed")
    client = coval_agent_sdk.CovalVoiceClient(
        api_key="synthetic-key", trace_exporter=mock.Mock()
    )
    client.trace_exporter.flush.return_value = True
    client.trace_exporter.discard.side_effect = RuntimeError(
        "secondary discard failure"
    )
    sockets = setup_run(monkeypatch, client, status)
    cleaned = []

    async def application(connection):
        try:
            if phase == "start":
                raise original
            async for _ in connection.receive_audio():
                if phase == "input":
                    raise original
                await connection.send_audio(OUTPUT)
                if phase == "output":
                    raise original
        finally:
            cleaned.append(connection.simulation_id)
            if phase == "cleanup":
                raise original

    async def direct():
        async with client.connect("conversation-a") as connection:
            await application(connection)

    operation = (
        direct()
        if entry == "connect"
        else client.run(
            coval_agent_sdk.RunConfig("agent-a", "persona-a", "test-set-a"),
            application,
        )
    )
    with pytest.raises(RuntimeError) as raised:
        await asyncio.wait_for(operation, 2)
    assert raised.value is original
    assert not isinstance(raised.value, coval_agent_sdk.CovalError)
    assert cleaned == ["conversation-a"]
    assert sockets["conversation-a"].sent.is_set() == (
        phase in {"output", "cleanup"}
    )
    assert sockets["conversation-a"].closed
    client.trace_exporter.upload.assert_not_called()
    client.trace_exporter.discard.assert_called_once_with(
        simulation_id="conversation-a"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["connect", "run"])
async def test_cancellation_awaits_application_cleanup(monkeypatch, entry):
    started, cleaned = asyncio.Event(), asyncio.Event()
    client = coval_agent_sdk.CovalVoiceClient(
        api_key="synthetic-key", trace_exporter=mock.Mock()
    )
    client.trace_exporter.flush.return_value = True
    client.trace_exporter.discard.side_effect = RuntimeError("discard failed")
    sockets = setup_run(monkeypatch, client, "RUNNING")

    async def application(connection):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    async def direct():
        async with client.connect("conversation-a") as connection:
            await application(connection)

    operation = (
        direct()
        if entry == "connect"
        else client.run(
            coval_agent_sdk.RunConfig("agent-a", "persona-a", "test-set-a"),
            application,
        )
    )
    task = asyncio.create_task(operation)
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
    assert sockets["conversation-a"].closed
    client.trace_exporter.upload.assert_not_called()
    client.trace_exporter.discard.assert_called_once_with(
        simulation_id="conversation-a"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_kind", ["application", "sdk", "cancellation", "relay"]
)
async def test_failure_cancels_and_awaits_real_sibling_handlers(
    monkeypatch, failure_kind
):
    client = coval_agent_sdk.CovalVoiceClient(api_key="synthetic-key")
    sockets = setup_run(monkeypatch, client, "RUNNING", ("broken", "waiting"))
    sibling_started, sibling_cleaned = asyncio.Event(), asyncio.Event()
    broken_cleaned = asyncio.Event()
    if failure_kind == "relay":

        class BrokenSocket(Socket):
            async def __aiter__(self):
                yield READY
                await sibling_started.wait()
                yield '{"type":"error","message":"synthetic relay failure"}'

        async def connect(api, simulation_id):
            sockets[simulation_id] = (
                BrokenSocket() if simulation_id == "broken" else Socket()
            )
            return sockets[simulation_id]

        monkeypatch.setattr(_connection, "open_voice_connection", connect)
    original = (
        coval_agent_sdk.CovalError("SDK failed")
        if failure_kind == "sdk"
        else asyncio.CancelledError("stopped")
        if failure_kind == "cancellation"
        else RuntimeError("application failed")
    )

    async def application(connection):
        if connection.simulation_id == "broken":
            await sibling_started.wait()
            if failure_kind == "relay":
                try:
                    await asyncio.Event().wait()
                finally:
                    broken_cleaned.set()
            raise original
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            sibling_cleaned.set()

    expected = (
        coval_agent_sdk.RunError
        if failure_kind in {"sdk", "relay"}
        else type(original)
    )
    with pytest.raises(expected) as raised:
        await asyncio.wait_for(
            client.run(
                coval_agent_sdk.RunConfig("agent-a", "persona-a", "test-set-a"),
                application,
            ),
            2,
        )
    if failure_kind == "application":
        assert raised.value is original
    if failure_kind == "sdk":
        assert raised.value.__cause__ is original
    assert sibling_cleaned.is_set()
    if failure_kind == "relay":
        assert broken_cleaned.is_set()
    assert all(socket.closed for socket in sockets.values())


@pytest.mark.asyncio
async def test_early_handler_return_fails_without_claiming_completed_session(
    monkeypatch,
):
    client = coval_agent_sdk.CovalVoiceClient(api_key="synthetic-key")
    sockets = setup_run(monkeypatch, client)

    async def early(connection):
        return None

    with pytest.raises(
        coval_agent_sdk.RunError, match="before the conversation completed"
    ) as raised:
        await asyncio.wait_for(
            client.run(
                coval_agent_sdk.RunConfig("agent-a", "persona-a", "test-set-a"),
                early,
            ),
            2,
        )
    assert raised.value.result.conversations == ()
    assert sockets["conversation-a"].closed


@pytest.mark.asyncio
async def test_later_page_failure_cleans_existing_handler(
    monkeypatch,
):
    client = coval_agent_sdk.CovalVoiceClient(api_key="synthetic-key")
    sockets = setup_run(monkeypatch, client, "RUNNING")
    started, cleaned = asyncio.Event(), asyncio.Event()
    query_seen = []

    async def read(path, *, query=None):
        query_seen.append(query)
        assert path == "simulations"
        if "page_token" not in query:
            return {
                "simulations": [
                    {"simulation_id": "conversation-a", "status": "RUNNING"}
                ],
                "next_page_token": "next-page",
            }
        await started.wait()
        raise coval_agent_sdk.CovalHttpError(
            status_code=403, message="page rejected"
        )

    async def application(connection):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(client._api, "get", read)
    with pytest.raises(coval_agent_sdk.RunError) as raised:
        await asyncio.wait_for(
            client.run(
                coval_agent_sdk.RunConfig("agent-a", "persona-a", "test-set-a"),
                application,
            ),
            2,
        )
    assert raised.value.result.run_id == "run-a"
    assert cleaned.is_set()
    assert sockets["conversation-a"].closed
    assert query_seen == [
        {"filter": 'run_id="run-a"', "page_size": "100"},
        {
            "filter": 'run_id="run-a"',
            "page_size": "100",
            "page_token": "next-page",
        },
    ]


@pytest.mark.asyncio
async def test_real_conversation_span_ends_before_optional_upload(monkeypatch):
    from opentelemetry.sdk import trace as sdk_trace
    from opentelemetry.sdk.trace import export as trace_export

    from coval_agent_sdk import tracing

    exporter = tracing.CovalTraceExporter()
    provider = sdk_trace.TracerProvider()
    provider.add_span_processor(trace_export.SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing.trace, "get_tracer_provider", lambda: provider)
    upload = mock.Mock()
    monkeypatch.setattr(tracing, "_upload_spans", upload)
    client = coval_agent_sdk.CovalVoiceClient(
        api_key="synthetic", trace_exporter=exporter
    )
    setup_run(monkeypatch, client)
    try:
        async with client.connect("conversation-a") as connection:
            span = provider.get_tracer(__name__).start_span(
                "conversation",
                attributes={"conversation.id": connection.simulation_id},
            )
            try:
                await handle(connection)
                upload.assert_not_called()
            finally:
                span.end()
        assert connection.result.trace == coval_agent_sdk.TraceResult(
            uploaded_spans=1
        )
        delivered = upload.call_args.args[0]
        assert [s.name for s in delivered] == ["conversation"]
        assert delivered[0].attributes["conversation.id"] == "conversation-a"
        assert delivered[0].end_time is not None
    finally:
        provider.shutdown()


@pytest.mark.asyncio
async def test_polling_sleep_cannot_outlive_short_run_deadline(monkeypatch):
    client = coval_agent_sdk.CovalVoiceClient(api_key="synthetic")
    monkeypatch.setattr(
        client._api,
        "post",
        mock.AsyncMock(return_value={"run": {"run_id": "run-a"}}),
    )

    async def read(path, *, query=None):
        if path == "simulations":
            return {
                "simulations": [
                    {"simulation_id": "conversation-a", "status": "PENDING"}
                ]
            }
        return {"run": {"status": "RUNNING"}}

    monkeypatch.setattr(client._api, "get", read)
    monkeypatch.setattr(_defaults, "POLL_INTERVAL_SECONDS", 2)
    handler = mock.AsyncMock()
    with pytest.raises(
        coval_agent_sdk.RunError, match="did not finish within"
    ) as raised:
        await asyncio.wait_for(
            client.run(
                coval_agent_sdk.RunConfig(
                    "agent-a", "persona-a", "set-a", run_timeout_seconds=0.01
                ),
                handler,
            ),
            1,
        )
    assert raised.value.result == coval_agent_sdk.RunResult(
        "run-a", "RUNNING", ()
    )
    handler.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("application_failure", [False, True])
async def test_provider_flush_failure_preserves_voice_or_application_error(
    monkeypatch, caplog, capsys, application_failure
):
    from opentelemetry.sdk import trace as sdk_trace
    from opentelemetry.sdk.trace import export as trace_export

    from coval_agent_sdk import tracing

    class FailingProcessor(sdk_trace.SpanProcessor):
        def on_start(self, *args, **kwargs):
            pass

        def on_end(self, *args, **kwargs):
            pass

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            raise RuntimeError("synthetic processor flush failed")

    exporter = tracing.CovalTraceExporter()
    provider = sdk_trace.TracerProvider()
    provider.add_span_processor(trace_export.SimpleSpanProcessor(exporter))
    provider.add_span_processor(FailingProcessor())
    monkeypatch.setattr(tracing.trace, "get_tracer_provider", lambda: provider)
    upload = mock.Mock()
    monkeypatch.setattr(tracing, "_upload_spans", upload)
    client = coval_agent_sdk.CovalVoiceClient(
        api_key="synthetic", trace_exporter=exporter
    )
    sockets = setup_run(monkeypatch, client)
    original = RuntimeError("application failed")

    async def connect():
        async with client.connect("conversation-a") as connection:
            span = provider.get_tracer(__name__).start_span(
                "conversation",
                attributes={"conversation.id": connection.simulation_id},
            )
            try:
                await handle(connection)
                if application_failure:
                    raise original
            finally:
                span.end()
        return connection.result

    try:
        if application_failure:
            with pytest.raises(RuntimeError) as raised:
                await connect()
            assert raised.value is original
            assert exporter._buffered_span_count == 0
        else:
            result = await connect()
            assert result.status == "COMPLETED"
            assert result.trace.uploaded_spans is None
            assert "synthetic processor flush failed" in result.trace.error
            upload.assert_not_called()
            assert (
                exporter.upload(
                    simulation_id="conversation-a",
                    api_base_url="https://api.coval.dev/v1",
                    api_key="synthetic",
                )
                == 1
            )
        assert sockets["conversation-a"].closed
        assert caplog.records == []
        assert capsys.readouterr() == ("", "")
    finally:
        provider.shutdown()


@pytest.mark.asyncio
async def test_custom_exporter_error_keeps_application_exception_identity(
    monkeypatch,
):
    original = RuntimeError("custom exporter failed")
    exporter = mock.Mock()
    exporter.flush.side_effect = original
    client = coval_agent_sdk.CovalVoiceClient(
        api_key="synthetic", trace_exporter=exporter
    )
    sockets = setup_run(monkeypatch, client)
    with pytest.raises(RuntimeError) as raised:
        async with client.connect("conversation-a") as connection:
            await handle(connection)
    assert raised.value is original
    assert connection.result is None
    assert sockets["conversation-a"].closed


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent_id", ""),
        ("persona_id", ""),
        ("test_set_id", ""),
        ("iterations", 0),
        ("concurrency", 0),
        ("run_timeout_seconds", 0),
        ("run_timeout_seconds", -1),
        ("run_timeout_seconds", float("inf")),
        ("run_timeout_seconds", float("nan")),
    ],
)
def test_invalid_public_run_configuration_is_rejected(field, value):
    values = {
        "agent_id": "agent-a",
        "persona_id": "persona-a",
        "test_set_id": "set-a",
    }
    values[field] = value
    with pytest.raises(ValueError, match=field):
        coval_agent_sdk.RunConfig(**values)
