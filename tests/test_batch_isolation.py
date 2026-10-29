"""A rejected session cannot abandon unrelated or queued conversations."""

import asyncio
import dataclasses
import json
from unittest import mock

import pytest
from websockets import exceptions
from websockets import frames

import coval_agent_sdk
from coval_agent_sdk import _connection
from coval_agent_sdk import _defaults
from coval_agent_sdk import _session

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


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [3, 75])
async def test_1013_keeps_current_and_later_queued_sessions_alive(
    monkeypatch, count, caplog, capsys
):
    completed, started, cleaned, sockets = set(), set(), set(), {}
    cancelled = set()
    healthy_started, late_started = asyncio.Event(), asyncio.Event()
    client = coval_agent_sdk.CovalVoiceClient(api_key="synthetic-key")

    class Socket:
        def __init__(self, simulation_id):
            self.simulation_id = simulation_id
            self.sent = asyncio.Event()
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        async def __aiter__(self):
            yield READY
            if self.simulation_id == "session-0":
                await healthy_started.wait()
                # The hosted record can complete despite an abnormal close.
                completed.add(self.simulation_id)
                raise exceptions.ConnectionClosedError(
                    frames.Close(1013, "synthetic overload"), None
                )
            yield PCM
            await self.sent.wait()
            completed.add(self.simulation_id)
            yield '{"type":"session_end"}'

        async def send(self, message):
            if isinstance(message, bytes):
                assert message == PCM
                self.sent.set()

    async def connect(api, simulation_id):
        assert simulation_id not in sockets
        sockets[simulation_id] = Socket(simulation_id)
        return sockets[simulation_id]

    async def read(path, *, query=None):
        if path == "simulations":
            return {
                "simulations": [
                    {
                        "simulation_id": f"session-{index}",
                        "status": "IN QUEUE"
                        if index >= 2 and "session-0" not in completed
                        else "RUNNING",
                    }
                    for index in range(count)
                ]
            }
        assert path == "runs/run-batch"
        return {
            "run": {
                "status": "COMPLETED" if len(completed) == count else "RUNNING"
            }
        }

    async def application(connection):
        simulation_id = connection.simulation_id
        started.add(simulation_id)
        if simulation_id == "session-1":
            healthy_started.set()
        elif simulation_id not in {"session-0", "session-1"}:
            late_started.set()
        try:
            if simulation_id == "session-1":
                await late_started.wait()
            async for audio in connection.receive_audio():
                await connection.send_audio(audio)
        except asyncio.CancelledError:
            cancelled.add(simulation_id)
            raise
        finally:
            cleaned.add(simulation_id)

    launch = mock.AsyncMock(return_value={"run": {"run_id": "run-batch"}})
    monkeypatch.setattr(client._api, "post", launch)
    monkeypatch.setattr(client._api, "get", read)
    monkeypatch.setattr(_connection, "open_voice_connection", connect)
    monkeypatch.setattr(_defaults, "POLL_INTERVAL_SECONDS", 0)
    with pytest.raises(coval_agent_sdk.RunError) as raised:
        await asyncio.wait_for(
            client.run(
                coval_agent_sdk.RunConfig(
                    "agent", "persona", "set", run_timeout_seconds=3
                ),
                application,
            ),
            4,
        )
    result = raised.value.result
    assert result.status == "COMPLETED"
    assert len(result.conversations) == count - 1
    assert {r.simulation_id for r in result.conversations} == (
        completed - {"session-0"}
    )
    assert len(result.failures) == 1
    failure = result.failures[0]
    assert failure.simulation_id == "session-0"
    assert failure.error_type == "CovalError"
    assert "1013" in failure.message
    assert cancelled <= {"session-0"}
    assert len(started) == count
    assert cleaned == started
    assert all(socket.closed for socket in sockets.values())
    launch.assert_awaited_once()
    with pytest.raises(dataclasses.FrozenInstanceError):
        failure.message = "hidden"
    assert capsys.readouterr() == ("", "")
    assert caplog.records == []


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [True, False])
async def test_all_failed_or_deadline_preserves_session_diagnostics(
    monkeypatch, terminal
):
    cancelled = asyncio.Event()
    client = coval_agent_sdk.CovalVoiceClient(api_key="synthetic-key")

    async def read(path, *, query=None):
        if path == "simulations":
            return {
                "simulations": [
                    {"simulation_id": name, "status": "RUNNING"}
                    for name in ("failed", "other")
                ]
            }
        return {"run": {"status": "FAILED" if terminal else "RUNNING"}}

    async def session(api, simulation_id, handler, exporter):
        if terminal or simulation_id == "failed":
            raise coval_agent_sdk.CovalError("synthetic session error")
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(
        client._api,
        "post",
        mock.AsyncMock(return_value={"run": {"run_id": "run-batch"}}),
    )
    monkeypatch.setattr(client._api, "get", read)
    monkeypatch.setattr(_session, "run_session", session)
    monkeypatch.setattr(_defaults, "POLL_INTERVAL_SECONDS", 0)
    with pytest.raises(coval_agent_sdk.RunError) as raised:
        await asyncio.wait_for(
            client.run(
                coval_agent_sdk.RunConfig(
                    "agent", "persona", "set", run_timeout_seconds=0.05
                ),
                mock.AsyncMock(),
            ),
            1,
        )
    assert len(raised.value.result.failures) == (2 if terminal else 1)
    assert raised.value.result.conversations == ()
    assert cancelled.is_set() is not terminal
    if not terminal:
        assert raised.value.result.status == "RUNNING"
        assert any(word in str(raised.value) for word in ("within", "deadline"))
