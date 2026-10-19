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
        client.api,
        "post",
        mock.AsyncMock(return_value={"run": {"run_id": "run-a"}}),
    )
    monkeypatch.setattr(client.api, "get", read)
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
        exporter.upload.side_effect = tracing.CovalTraceError("upload rejected")
    provider = mock.Mock()
    provider.force_flush.return_value = trace_failure != "flush"
    monkeypatch.setattr(_session.trace, "get_tracer_provider", lambda: provider)
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
            client.api, "post", mock.AsyncMock(side_effect=original)
        ),
        pytest.raises(coval_agent_sdk.CovalHttpError) as failure,
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
            client.api,
            "post",
            mock.AsyncMock(return_value={"run": {"run_id": "run-a"}}),
        ),
        mock.patch.object(
            client.api, "get", mock.AsyncMock(side_effect=failure)
        ),
        pytest.raises(coval_agent_sdk.RunError) as raised,
    ):
        await client.run(
            coval_agent_sdk.RunConfig("agent-a", "persona-a", "test-set-a"),
            lambda _: Agent(),
        )
    assert raised.value.result == coval_agent_sdk.RunResult("run-a", None, ())
    assert raised.value.__cause__ is failure
    assert capsys.readouterr() == ("", "")
