"""Behavior of independently produced audio through the public voice client."""

import asyncio
import json
import subprocess
import sys
import types
from unittest import mock

from opentelemetry.sdk import trace as sdk_trace
from opentelemetry.sdk.trace import export as trace_export
import pytest
import urllib3
from urllib3 import exceptions as http_exceptions
from websockets import exceptions
from websockets import frames

import coval_agent_sdk
from coval_agent_sdk import _audio
from coval_agent_sdk import _connection
from coval_agent_sdk import _defaults
from coval_agent_sdk import tracing

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
GREETING = b"\x01\x00" * 320
REPLY = b"\x02\x00" * 320


class StreamingAgent(coval_agent_sdk.StreamingVoiceAgent):
    def __init__(self):
        self.output = asyncio.Queue(maxsize=4)
        self.closed = False
        self.stream_closed = False
        self.stream_started = asyncio.Event()
        self.received = []

    async def on_ready(self):
        self.output.put_nowait(GREETING)

    async def on_audio(self, audio):
        self.received.append(audio)
        self.output.put_nowait(REPLY)

    async def audio_output(self):
        self.stream_started.set()
        try:
            yield b""
            while True:
                yield await self.output.get()
        finally:
            self.stream_closed = True

    async def close(self):
        self.closed = True


class Socket:
    def __init__(self):
        self.messages = asyncio.Queue()
        self.sent = []
        self.greeting_sent = asyncio.Event()
        self.reply_sent = asyncio.Event()
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.messages.get()
        if message is None:
            raise StopAsyncIteration
        if isinstance(message, BaseException):
            raise message
        return message

    async def send(self, message):
        self.sent.append(message)
        if message == GREETING:
            self.greeting_sent.set()
        if message == REPLY:
            self.reply_sent.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["ready", "audio"])
@pytest.mark.parametrize("output", ["xx", [1, 2], b"\x00"])
async def test_callback_output_is_validated_before_sending(
    monkeypatch, phase, output
):
    class Agent:
        closed = False

        async def on_ready(self):
            return output if phase == "ready" else None

        async def on_audio(self, audio):
            return output

        async def close(self):
            self.closed = True

    agent, socket = Agent(), Socket()
    socket.messages.put_nowait(READY)
    socket.messages.put_nowait(b"\x01\x00" * 320)
    monkeypatch.setattr(
        _connection,
        "open_voice_connection",
        mock.AsyncMock(return_value=socket),
    )
    with pytest.raises(coval_agent_sdk.CovalError, match="PCM16"):
        await asyncio.wait_for(
            coval_agent_sdk.CovalVoiceClient(api_key="synthetic").connect(
                "simulation-a", lambda _: agent
            ),
            2,
        )
    assert not any(isinstance(message, bytes) for message in socket.sent)
    assert not any(message == output for message in socket.sent)
    assert agent.closed
    assert socket.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("messages", "diagnostic", "ready_count"),
    [
        (["{"], "invalid control JSON", 0),
        (["[]"], "invalid control JSON", 0),
        ([READY, "{"], "invalid control JSON", 1),
        (['{"type":"session_end"}'], "before session_ready", 0),
        ([READY, READY], "repeated session_ready", 1),
    ],
)
async def test_invalid_control_sequence_never_reports_success(
    monkeypatch, messages, diagnostic, ready_count
):
    class Agent(StreamingAgent):
        ready_calls = 0

        async def on_ready(self):
            self.ready_calls += 1
            await super().on_ready()

    agent, socket = Agent(), Socket()
    for message in messages:
        socket.messages.put_nowait(message)
    exporter = mock.Mock()
    exporter.flush.return_value = True
    monkeypatch.setattr(
        _connection,
        "open_voice_connection",
        mock.AsyncMock(return_value=socket),
    )
    with pytest.raises(coval_agent_sdk.CovalError, match=diagnostic):
        await asyncio.wait_for(
            coval_agent_sdk.CovalVoiceClient(
                api_key="synthetic", trace_exporter=exporter
            ).connect("simulation-a", lambda _: agent),
            2,
        )
    assert agent.ready_calls == ready_count
    assert agent.closed
    assert socket.closed
    exporter.upload.assert_not_called()
    exporter.discard.assert_called_once_with(simulation_id="simulation-a")


@pytest.mark.asyncio
async def test_stream_close_preserves_original_output_failure(monkeypatch):
    original = RuntimeError("output failed")

    class Stream:
        closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise original

        async def aclose(self):
            self.closed = True
            raise RuntimeError("iterator cleanup failed")

    stream = Stream()

    class Agent(StreamingAgent):
        def audio_output(self):
            return stream

    agent, socket = Agent(), Socket()
    socket.messages.put_nowait(READY)
    monkeypatch.setattr(
        _connection,
        "open_voice_connection",
        mock.AsyncMock(return_value=socket),
    )
    with pytest.raises(RuntimeError) as failure:
        await asyncio.wait_for(
            coval_agent_sdk.CovalVoiceClient(api_key="synthetic").connect(
                "simulation-a", lambda _: agent
            ),
            2,
        )
    assert failure.value is original
    assert stream.closed
    assert agent.closed
    assert socket.closed


@pytest.mark.asyncio
async def test_greeting_and_reply_stream_during_input_silence_and_close(
    monkeypatch,
):
    socket = Socket()
    socket.messages.put_nowait(READY)
    agent = StreamingAgent()
    monkeypatch.setattr(
        _connection,
        "open_voice_connection",
        mock.AsyncMock(return_value=socket),
    )
    voice = coval_agent_sdk.CovalVoiceClient(api_key="synthetic-key")
    session = asyncio.create_task(
        voice.connect("simulation-a", lambda _: agent)
    )
    try:
        await asyncio.wait_for(socket.greeting_sent.wait(), 1)
        assert agent.received == []
        socket.messages.put_nowait(b"\x03\x00" * 320)
        await asyncio.wait_for(socket.reply_sent.wait(), 1)
        assert agent.received == [b"\x03\x00" * 320]
        assert socket.sent == [
            GREETING,
            '{"type": "audio_ack", "received_bytes": 640}',
            REPLY,
        ]
        socket.messages.put_nowait('{"type":"session_end"}')
        await asyncio.wait_for(session, 1)
    finally:
        session.cancel()
        await asyncio.gather(session, return_exceptions=True)
    assert agent.closed
    assert agent.stream_closed
    assert socket.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["streaming", "callback"])
async def test_back_to_back_agent_chunks_retain_the_audio_pacing_budget(
    monkeypatch, mode
):
    clock = [10.0]
    sent_at = []
    real_asyncio = asyncio
    sdk_asyncio = types.ModuleType("sdk_asyncio")
    sdk_asyncio.__dict__.update(vars(asyncio))

    async def sleep(delay):
        clock[0] += delay
        await real_asyncio.sleep(0)

    sdk_asyncio.sleep = sleep
    monkeypatch.setattr(_audio, "asyncio", sdk_asyncio)
    monkeypatch.setattr(
        _audio, "time", types.SimpleNamespace(monotonic=lambda: clock[0])
    )

    class BurstAgent(StreamingAgent):
        async def audio_output(self):
            yield GREETING
            yield REPLY
            await asyncio.Event().wait()

    class TimedSocket(Socket):
        async def send(self, message):
            if isinstance(message, bytes):
                sent_at.append(clock[0])
            await super().send(message)

    socket = TimedSocket()
    socket.messages.put_nowait(READY)

    class CallbackAgent:
        closed = False

        async def on_ready(self):
            return GREETING

        async def on_audio(self, audio):
            return REPLY

        async def close(self):
            self.closed = True

    if mode == "callback":
        socket.messages.put_nowait(b"\x00\x00" * 320)
    agent = BurstAgent() if mode == "streaming" else CallbackAgent()
    monkeypatch.setattr(
        _connection,
        "open_voice_connection",
        mock.AsyncMock(return_value=socket),
    )
    session = asyncio.create_task(
        coval_agent_sdk.CovalVoiceClient(api_key="key").connect(
            "simulation-a", lambda _: agent
        )
    )
    try:
        await asyncio.wait_for(socket.reply_sent.wait(), 1)
        assert [m for m in socket.sent if isinstance(m, bytes)] == [
            GREETING,
            REPLY,
        ]
        assert sent_at[1] - sent_at[0] == pytest.approx(0.02)
        socket.messages.put_nowait('{"type":"session_end"}')
        await asyncio.wait_for(session, 1)
    finally:
        session.cancel()
        await asyncio.gather(session, return_exceptions=True)
    assert agent.closed
    assert socket.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["producer", "end", "odd", "oversize"])
async def test_output_failure_wakes_the_receiver_and_closes_resources(
    monkeypatch, failure
):
    class FailingAgent(StreamingAgent):
        async def audio_output(self):
            if failure == "producer":
                raise RuntimeError("synthetic producer failure")
            if failure == "odd":
                yield b"x"
            if failure == "oversize":
                yield bytes(_defaults.MAX_PENDING_REPLY_BYTES + 2)

    socket = Socket()
    socket.messages.put_nowait(READY)
    agent = FailingAgent()
    monkeypatch.setattr(
        _connection,
        "open_voice_connection",
        mock.AsyncMock(return_value=socket),
    )
    message = {
        "producer": "synthetic producer failure",
        "end": "audio stream ended",
        "odd": "even-length PCM16",
        "oversize": "exceeds the 2 MiB limit",
    }[failure]
    with pytest.raises(
        (RuntimeError, coval_agent_sdk.TunnelError), match=message
    ):
        await asyncio.wait_for(
            coval_agent_sdk.CovalVoiceClient(api_key="key").connect(
                "simulation-a", lambda _: agent
            ),
            1,
        )
    assert socket.sent == []
    assert agent.closed
    assert socket.closed


@pytest.mark.asyncio
async def test_cancellation_stops_a_blocked_audio_stream_and_closes_the_agent(
    monkeypatch,
):
    socket = Socket()
    socket.messages.put_nowait(READY)
    agent = StreamingAgent()
    monkeypatch.setattr(
        _connection,
        "open_voice_connection",
        mock.AsyncMock(return_value=socket),
    )
    session = asyncio.create_task(
        coval_agent_sdk.CovalVoiceClient(api_key="key").connect(
            "simulation-a", lambda _: agent
        )
    )
    await asyncio.wait_for(agent.stream_started.wait(), 1)
    session.cancel()
    with pytest.raises(asyncio.CancelledError):
        await session
    assert agent.closed
    assert agent.stream_closed
    assert socket.closed


@pytest.mark.asyncio
async def test_custom_workspace_is_forwarded_on_reads_and_run_launches(
    monkeypatch,
):
    requests = []

    def request(self, method, url, **kwargs):
        requests.append((method, url, kwargs))
        return urllib3.HTTPResponse(b'{"ok": true}', status=200)

    monkeypatch.setattr(urllib3.PoolManager, "request", request)
    voice = coval_agent_sdk.CovalVoiceClient(
        api_key="synthetic-key", workspace_id="workspace-a"
    )
    await voice._api.get("simulations/simulation-a/connect")
    await voice._api.post("runs", {"agent_id": "agent-a"})
    assert [item[0] for item in requests] == ["GET", "POST"]
    for _, _, options in requests:
        headers = {
            key.lower(): value for key, value in options["headers"].items()
        }
        assert headers["x-coval-workspace-id"] == "workspace-a"
        assert headers["x-api-key"] == "synthetic-key"
    assert json.loads(requests[1][2]["body"]) == {"agent_id": "agent-a"}


def test_public_package_imports_its_own_tracing_module(tmp_path):
    code = """
import sys
import types
sys.modules['tracing'] = types.ModuleType('consumer_owned_tracing')
from coval_agent_sdk import tracing
assert (
    tracing.CovalTraceExporter.__module__ == 'coval_agent_sdk.tracing'
)
"""
    subprocess.run(  # noqa: S603 - Fixed interpreter and source, no user input.
        [sys.executable, "-I", "-c", code], cwd=tmp_path, check=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("abnormal_close", [False, True])
async def test_missing_terminal_frame_confirms_completion_and_uploads(
    monkeypatch, abnormal_close
):
    socket = Socket()
    socket.messages.put_nowait(READY)
    exporter = tracing.CovalTraceExporter()
    provider = sdk_trace.TracerProvider()
    provider.add_span_processor(trace_export.SimpleSpanProcessor(exporter))
    span = provider.get_tracer(__name__).start_span(
        "conversation", attributes={"conversation.id": "simulation-a"}
    )
    upload = mock.Mock()
    monkeypatch.setattr(tracing, "_upload_spans", upload)
    monkeypatch.setattr(tracing.trace, "get_tracer_provider", lambda: provider)
    monkeypatch.setattr(_defaults, "POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(
        _connection,
        "open_voice_connection",
        mock.AsyncMock(return_value=socket),
    )
    requests = []
    states = iter(["IN PROGRESS", "COMPLETED"])

    def request(self, method, url, **kwargs):
        requests.append((url, kwargs["headers"]))
        return urllib3.HTTPResponse(
            json.dumps({"simulation": {"status": next(states)}}).encode(),
            status=200,
        )

    monkeypatch.setattr(urllib3.PoolManager, "request", request)

    class TracedAgent(StreamingAgent):
        async def close(self):
            span.end()
            await super().close()

    agent = TracedAgent()
    voice = coval_agent_sdk.CovalVoiceClient(
        api_key="synthetic-key",
        workspace_id="workspace-a",
        trace_exporter=exporter,
    )
    session = asyncio.create_task(
        voice.connect("simulation-a", lambda _: agent)
    )
    try:
        await asyncio.wait_for(socket.greeting_sent.wait(), 2)
        socket.messages.put_nowait(
            exceptions.ConnectionClosedError(None, None)
            if abnormal_close
            else None
        )
        await asyncio.wait_for(session, 2)
        assert agent.closed
        assert agent.stream_closed
        assert socket.closed
        assert len(requests) == 2
        for url, headers in requests:
            assert url.endswith("/simulations/simulation-a")
            assert headers["X-Coval-Workspace-Id"] == "workspace-a"
        assert upload.call_args.kwargs["workspace_id"] == "workspace-a"
        assert [span.name for span in upload.call_args.args[0]] == [
            "conversation"
        ]
    finally:
        session.cancel()
        await asyncio.gather(session, return_exceptions=True)
        provider.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        "FAILED",
        "CANCELLED",
        "PENDING",
        "MALFORMED",
        "INVALID_JSON",
        "UNREACHABLE",
        "UNAUTHORIZED",
    ],
)
async def test_socket_failure_is_preserved_without_authoritative_completion(
    monkeypatch, outcome
):
    socket = Socket()
    socket.messages.put_nowait(READY)
    monkeypatch.setattr(
        _connection,
        "open_voice_connection",
        mock.AsyncMock(return_value=socket),
    )
    monkeypatch.setattr(_defaults, "CLOSE_CONFIRM_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(_defaults, "POLL_INTERVAL_SECONDS", 0)
    requests = []

    def request(self, method, url, **kwargs):
        requests.append(url)
        if outcome == "UNREACHABLE":
            raise http_exceptions.NewConnectionError(None, "synthetic failure")
        if outcome == "UNAUTHORIZED":
            return urllib3.HTTPResponse(b"denied", status=401)
        if outcome == "INVALID_JSON":
            return urllib3.HTTPResponse(b"not-json", status=200)
        payload = (
            {}
            if outcome == "MALFORMED"
            else {"simulation": {"status": outcome}}
        )
        return urllib3.HTTPResponse(json.dumps(payload).encode(), status=200)

    monkeypatch.setattr(urllib3.PoolManager, "request", request)
    agent = StreamingAgent()
    exporter = mock.Mock()
    session = asyncio.create_task(
        coval_agent_sdk.CovalVoiceClient(
            api_key="key", trace_exporter=exporter
        ).connect("simulation-a", lambda _: agent)
    )
    try:
        await asyncio.wait_for(socket.greeting_sent.wait(), 2)
        socket.messages.put_nowait(exceptions.ConnectionClosedError(None, None))
        with pytest.raises(exceptions.ConnectionClosedError):
            await asyncio.wait_for(session, 2)
        assert requests
        assert agent.closed
        assert agent.stream_closed
        assert socket.closed
        exporter.upload.assert_not_called()
        exporter.discard.assert_called_once_with(simulation_id="simulation-a")
    finally:
        session.cancel()
        await asyncio.gather(session, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("close_code", [1008, 1013])
async def test_explicit_relay_failures_do_not_query_for_success(
    monkeypatch, close_code
):
    socket = Socket()
    socket.messages.put_nowait(READY)
    monkeypatch.setattr(
        _connection,
        "open_voice_connection",
        mock.AsyncMock(return_value=socket),
    )
    voice = coval_agent_sdk.CovalVoiceClient(api_key="key")
    read = mock.AsyncMock(return_value={"simulation": {"status": "COMPLETED"}})
    monkeypatch.setattr(voice._api, "get", read)
    agent = StreamingAgent()
    session = asyncio.create_task(
        voice.connect("simulation-a", lambda _: agent)
    )
    try:
        await asyncio.wait_for(socket.greeting_sent.wait(), 2)
        socket.messages.put_nowait(
            exceptions.ConnectionClosedError(
                frames.Close(close_code, "synthetic relay rejection"), None
            )
        )
        with pytest.raises(
            (exceptions.ConnectionClosedError, coval_agent_sdk.TunnelError)
        ):
            await asyncio.wait_for(session, 2)
        read.assert_not_awaited()
        assert agent.closed
        assert socket.closed
    finally:
        session.cancel()
        await asyncio.gather(session, return_exceptions=True)


@pytest.mark.asyncio
async def test_close_before_ready_cannot_claim_completion(
    monkeypatch,
):
    socket = Socket()
    socket.messages.put_nowait(exceptions.ConnectionClosedError(None, None))
    monkeypatch.setattr(
        _connection,
        "open_voice_connection",
        mock.AsyncMock(return_value=socket),
    )
    voice = coval_agent_sdk.CovalVoiceClient(api_key="key")
    read = mock.AsyncMock(return_value={"simulation": {"status": "COMPLETED"}})
    monkeypatch.setattr(voice._api, "get", read)
    agent = StreamingAgent()
    with pytest.raises(exceptions.ConnectionClosedError):
        await asyncio.wait_for(
            voice.connect("simulation-a", lambda _: agent), 2
        )
    read.assert_not_awaited()
    assert agent.closed
    assert socket.closed
    assert not agent.stream_started.is_set()


@pytest.mark.asyncio
async def test_cancel_during_verification_closes_and_discards(
    monkeypatch,
):
    socket = Socket()
    socket.messages.put_nowait(READY)
    monkeypatch.setattr(
        _connection,
        "open_voice_connection",
        mock.AsyncMock(return_value=socket),
    )
    verifying = asyncio.Event()

    async def read(path):
        verifying.set()
        await asyncio.Event().wait()

    exporter = mock.Mock()
    voice = coval_agent_sdk.CovalVoiceClient(
        api_key="key", trace_exporter=exporter
    )
    monkeypatch.setattr(voice._api, "get", read)
    agent = StreamingAgent()
    session = asyncio.create_task(
        voice.connect("simulation-a", lambda _: agent)
    )
    try:
        await asyncio.wait_for(socket.greeting_sent.wait(), 2)
        socket.messages.put_nowait(exceptions.ConnectionClosedError(None, None))
        await asyncio.wait_for(verifying.wait(), 2)
        session.cancel()
        with pytest.raises(asyncio.CancelledError):
            await session
        assert agent.closed
        assert agent.stream_closed
        assert socket.closed
        exporter.upload.assert_not_called()
        exporter.discard.assert_called_once_with(simulation_id="simulation-a")
    finally:
        session.cancel()
        await asyncio.gather(session, return_exceptions=True)
