"""Public outcomes and silence with real run/session orchestration."""

import asyncio
import dataclasses
import json
from unittest import mock

import pytest

import coval_agent_sdk
from coval_agent_sdk import _connection
from coval_agent_sdk import _defaults
from coval_agent_sdk import _session
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
INPUT = b"\x01\x00" * 320
OUTPUT = b"\x02\x00" * 320


class Agent:
    def __init__(self):
        self.closed = False

    async def on_ready(self):
        return None

    async def on_audio(self, audio):
        assert audio == INPUT
        return OUTPUT

    async def close(self):
        self.closed = True


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
        yield INPUT
        await self.sent.wait()
        yield '{"type":"session_end"}'

    async def send(self, value):
        if isinstance(value, bytes):
            assert value == OUTPUT
            self.sent.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("hosted_status", ["COMPLETED", "FAILED"])
async def test_run_returns_identity_and_sessions_without_output(
    monkeypatch, capsys, caplog, hosted_status
):
    caplog.set_level("DEBUG")
    client = coval_agent_sdk.CovalVoiceClient(api_key="synthetic-key")
    sockets = {}
    agents = {}

    async def connect(api, simulation_id):
        socket = sockets[simulation_id] = Socket()
        return socket

    def factory(simulation_id):
        agent = agents[simulation_id] = Agent()
        return agent

    async def read(path, *, query=None):
        await asyncio.sleep(0)
        if path == "simulations":
            return {
                "simulations": [
                    {"simulation_id": name, "status": "RUNNING"}
                    for name in ("conversation-a", "conversation-b")
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
    operation = client.run(
        coval_agent_sdk.RunConfig("agent-a", "persona-a", "test-set-a"), factory
    )
    if hosted_status == "FAILED":
        with pytest.raises(coval_agent_sdk.RunError) as failure:
            await asyncio.wait_for(operation, 2)
        result = failure.value.result
        assert isinstance(failure.value.__cause__, coval_agent_sdk.TunnelError)
    else:
        result = await asyncio.wait_for(operation, 2)
    assert result == coval_agent_sdk.RunResult(
        "run-a",
        hosted_status,
        (
            coval_agent_sdk.SessionResult("conversation-a", 640, 640),
            coval_agent_sdk.SessionResult("conversation-b", 640, 640),
        ),
    )
    assert all(agent.closed for agent in agents.values())
    assert all(socket.closed for socket in sockets.values())
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.run_id = "changed"
    assert capsys.readouterr() == ("", "")
    assert caplog.records == []


@pytest.mark.asyncio
@pytest.mark.parametrize("trace_failure", [None, "flush", "upload"])
async def test_connect_returns_trace_delivery_without_console_warnings(
    monkeypatch, capsys, caplog, trace_failure
):
    caplog.set_level("DEBUG")
    socket = Socket()
    agent = Agent()
    exporter = mock.Mock()
    exporter.upload.return_value = 7
    if trace_failure == "upload":
        exporter.upload.side_effect = errors.CovalTraceError("upload rejected")
    exporter.flush.return_value = trace_failure != "flush"
    monkeypatch.setattr(
        _connection,
        "open_voice_connection",
        mock.AsyncMock(return_value=socket),
    )
    client = coval_agent_sdk.CovalVoiceClient(
        api_key="synthetic-key", trace_exporter=exporter
    )
    result = await asyncio.wait_for(
        client.connect("conversation-a", lambda _: agent), 2
    )
    assert result.simulation_id == "conversation-a"
    assert result.status == "COMPLETED"
    assert (result.received_audio_bytes, result.sent_audio_bytes) == (640, 640)
    if trace_failure:
        assert result.trace.uploaded_spans is None
        assert result.trace.error
        assert (
            "flush" if trace_failure == "flush" else "upload rejected"
        ) in result.trace.error
    else:
        assert result.trace == coval_agent_sdk.TraceResult(uploaded_spans=7)
    exporter.discard.assert_not_called()
    assert agent.closed
    assert socket.closed
    assert capsys.readouterr() == ("", "")
    assert caplog.records == []


@pytest.mark.asyncio
async def test_launch_failure_keeps_original_error_and_stays_silent(capsys):
    client = coval_agent_sdk.CovalVoiceClient(api_key="synthetic-key")
    original = coval_agent_sdk.CovalHttpError(status_code=403, message="denied")
    with (
        mock.patch.object(
            client._api, "post", mock.AsyncMock(side_effect=original)
        ),
        pytest.raises(coval_agent_sdk.CovalError) as failure,
    ):
        await client.run(
            coval_agent_sdk.RunConfig("agent-a", "persona-a", "test-set-a"),
            lambda _: Agent(),
        )
    assert failure.value is original
    assert capsys.readouterr() == ("", "")


@pytest.mark.asyncio
async def test_post_launch_failure_retains_id_without_inventing_status(capsys):
    client = coval_agent_sdk.CovalVoiceClient(api_key="synthetic-key")
    failure = coval_agent_sdk.CovalHttpError(status_code=403, message="denied")
    with (
        mock.patch.object(
            client._api,
            "post",
            mock.AsyncMock(return_value={"run": {"run_id": "run-a"}}),
        ),
        mock.patch.object(
            client._api, "get", mock.AsyncMock(side_effect=failure)
        ),
        pytest.raises(coval_agent_sdk.CovalError) as raised,
    ):
        await client.run(
            coval_agent_sdk.RunConfig("agent-a", "persona-a", "test-set-a"),
            lambda _: Agent(),
        )
    assert isinstance(raised.value, coval_agent_sdk.RunError)
    assert raised.value.result == coval_agent_sdk.RunResult("run-a", None, ())
    assert raised.value.__cause__ is failure
    assert capsys.readouterr() == ("", "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry", "hosted_status"),
    [("connect", None), ("run", "RUNNING"), ("run", "COMPLETED")],
)
@pytest.mark.parametrize(
    "phase", ["factory", "ready", "audio", "output", "close"]
)
async def test_adapter_failures_keep_identity_after_cleanup(
    monkeypatch, entry, hosted_status, phase
):
    original = RuntimeError(f"adapter {phase} failed")
    failed = asyncio.Event()
    session_tasks = []
    socket = Socket()

    def fail():
        failed.set()
        raise original

    class FailingAgent(Agent):
        async def on_ready(self):
            if phase == "ready":
                fail()

        async def on_audio(self, audio):
            if phase == "audio":
                fail()
            return await super().on_audio(audio)

        async def close(self):
            await super().close()
            if phase == "close":
                fail()

    class FailingStream(coval_agent_sdk.StreamingVoiceAgent):
        def __init__(self):
            self.closed = False

        async def on_audio(self, audio):
            pass

        async def audio_output(self):
            yield b""
            fail()

        async def close(self):
            self.closed = True

    agent = FailingStream() if phase == "output" else FailingAgent()

    def factory(simulation_id):
        session_tasks.append(asyncio.current_task())
        assert simulation_id == "conversation-a"
        if phase == "factory":
            fail()
        return agent

    async def read(path, *, query=None):
        if path == "simulations":
            return {
                "simulations": [
                    {"simulation_id": "conversation-a", "status": "RUNNING"}
                ]
            }
        # Complete the task during this monitoring read. RUNNING exercises
        # polling; COMPLETED exercises the final task drain after the read.
        await failed.wait()
        await asyncio.wait(session_tasks)
        return {"run": {"status": hosted_status}}

    client = coval_agent_sdk.CovalVoiceClient(api_key="synthetic-key")
    monkeypatch.setattr(_defaults, "POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(
        _connection,
        "open_voice_connection",
        mock.AsyncMock(return_value=socket),
    )
    monkeypatch.setattr(
        client._api,
        "post",
        mock.AsyncMock(return_value={"run": {"run_id": "run-a"}}),
    )
    monkeypatch.setattr(client._api, "get", read)
    operation = (
        client.connect("conversation-a", factory)
        if entry == "connect"
        else client.run(
            coval_agent_sdk.RunConfig("agent-a", "persona-a", "test-set-a"),
            factory,
        )
    )
    with pytest.raises(RuntimeError) as failure:
        await asyncio.wait_for(operation, 2)
    assert failure.value is original
    assert not isinstance(failure.value, coval_agent_sdk.CovalError)
    assert agent.closed == (phase != "factory")
    assert socket.closed == (phase != "factory")


async def _native_cancellation(original):
    """Use native task behavior; older Python recreates cancellation errors."""

    async def fail():
        raise original

    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(fail(), 2)
    return caught.value


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["connect", "run"])
async def test_cancellation_propagates_after_cleanup_despite_discard_failure(
    monkeypatch, entry
):
    started = asyncio.Event()

    class WaitingAgent(Agent):
        async def on_ready(self):
            started.set()
            await asyncio.Event().wait()

    agent = WaitingAgent()
    socket = Socket()
    exporter = mock.Mock()
    exporter.flush.return_value = True
    exporter.discard.side_effect = RuntimeError("discard failed")
    client = coval_agent_sdk.CovalVoiceClient(
        api_key="synthetic-key", trace_exporter=exporter
    )

    async def read(path, *, query=None):
        if path == "simulations":
            return {
                "simulations": [
                    {"simulation_id": "conversation-a", "status": "RUNNING"}
                ]
            }
        await asyncio.Event().wait()
        return None

    monkeypatch.setattr(
        _connection,
        "open_voice_connection",
        mock.AsyncMock(return_value=socket),
    )
    monkeypatch.setattr(
        client._api,
        "post",
        mock.AsyncMock(return_value={"run": {"run_id": "run-a"}}),
    )
    monkeypatch.setattr(client._api, "get", read)
    operation = (
        client.connect("conversation-a", lambda _: agent)
        if entry == "connect"
        else client.run(
            coval_agent_sdk.RunConfig("agent-a", "persona-a", "test-set-a"),
            lambda _: agent,
        )
    )
    task = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel("stop requested")
        with pytest.raises(asyncio.CancelledError) as failure:
            await asyncio.wait_for(task, 2)
        control = await _native_cancellation(
            asyncio.CancelledError("stop requested")
        )
        assert failure.value.args == control.args
        assert not isinstance(failure.value, coval_agent_sdk.CovalError)
        assert agent.closed
        assert socket.closed
        exporter.discard.assert_called_once_with(simulation_id="conversation-a")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original",
    [
        RuntimeError("adapter failed"),
        asyncio.CancelledError("adapter stopped"),
        coval_agent_sdk.CovalTraceError("trace failed"),
    ],
)
async def test_failed_adapter_cancels_and_awaits_sibling_sessions(
    monkeypatch, original
):
    sibling_started = asyncio.Event()
    sibling_closed = asyncio.Event()

    async def session(api, simulation_id, agent_factory, trace_exporter):
        if simulation_id == "broken":
            await sibling_started.wait()
            raise original
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            sibling_closed.set()

    async def read(path, *, query=None):
        await asyncio.sleep(0)
        if path == "simulations":
            return {
                "simulations": [
                    {"simulation_id": name, "status": "RUNNING"}
                    for name in ("broken", "waiting")
                ]
            }
        return {"run": {"status": "RUNNING"}}

    client = coval_agent_sdk.CovalVoiceClient(api_key="synthetic-key")
    monkeypatch.setattr(_defaults, "POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(_session, "run_session", session)
    monkeypatch.setattr(
        client._api,
        "post",
        mock.AsyncMock(return_value={"run": {"run_id": "run-a"}}),
    )
    monkeypatch.setattr(client._api, "get", read)
    expected = (
        coval_agent_sdk.RunError
        if isinstance(original, coval_agent_sdk.CovalError)
        else type(original)
    )
    with pytest.raises(expected) as failure:
        await asyncio.wait_for(
            client.run(
                coval_agent_sdk.RunConfig("agent-a", "persona-a", "test-set-a"),
                lambda _: Agent(),
            ),
            2,
        )
    if isinstance(original, coval_agent_sdk.CovalError):
        assert failure.value.__cause__ is original
        assert failure.value.result == coval_agent_sdk.RunResult(
            "run-a", "RUNNING", ()
        )
    elif isinstance(original, asyncio.CancelledError):
        control_original = asyncio.CancelledError(*original.args)
        control = await _native_cancellation(control_original)
        assert failure.value.args == control.args
        assert (failure.value is original) == (control is control_original)
    else:
        assert failure.value is original
    assert sibling_closed.is_set()
