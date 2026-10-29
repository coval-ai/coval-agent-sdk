"""Public client journeys through loopback HTTP discovery and TLS WebSockets."""

import asyncio
import dataclasses
from http import server as http_server
import json
import shutil
import ssl
import subprocess
import threading
import time
from unittest import mock
from urllib import parse

import pytest
from websockets import protocol
from websockets.asyncio import server as websocket_server

import coval_agent_sdk
from coval_agent_sdk import _connection
from coval_agent_sdk import _defaults

IDS = ("conversation-a", "conversation-b")
CALLER_AUDIO = {IDS[0]: b"\x01\x00" * 320, IDS[1]: b"\x02\x00" * 320}
GREETING = b"\x03\x00" * 320
REPLY = b"\x04\x00" * 320
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


@pytest.fixture
def tls_contexts(tmp_path):
    openssl = shutil.which("openssl")
    assert openssl is not None, "Public TLS tests require openssl on PATH"
    certificate = tmp_path / "certificate.pem"
    private_key = tmp_path / "private-key.pem"
    subprocess.run(  # noqa: S603 - Fixed OpenSSL command creates ephemeral loopback credentials.
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-days",
            "1",
        ],
        check=True,
        capture_output=True,
    )
    serving = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    serving.load_cert_chain(certificate, private_key)
    trusting = ssl.create_default_context(cafile=str(certificate))
    return serving, trusting


@pytest.fixture
def api_endpoint():
    state = {
        "requests": [],
        "relay_url": None,
        "completed": set(),
        "offer_overrides": {},
        "ids": IDS,
        "queued_until": {},
    }

    class Handler(http_server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.respond()

        def do_GET(self):
            self.respond()

        def respond(self):
            target = parse.urlsplit(self.path)
            query = parse.parse_qs(target.query)
            headers = {
                key.lower(): value for key, value in self.headers.items()
            }
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state["requests"].append(
                (self.command, target.path, query, headers, body)
            )
            status = 200
            if headers.get("x-api-key") != "synthetic-key":
                status, response = 401, {"error": "authentication required"}
            elif headers.get("x-coval-workspace-id") != "workspace-wire":
                status, response = 403, {"error": "wrong workspace"}
            elif (self.command, target.path) == ("POST", "/v1/runs"):
                response = {"run": {"run_id": "run-wire"}}
            elif target.path == "/v1/simulations":
                second_page = "page_token" in query
                response = {
                    "simulations": [
                        {
                            "simulation_id": name,
                            "status": "IN QUEUE"
                            if name in state["queued_until"]
                            and state["queued_until"][name]
                            not in state["completed"]
                            else "RUNNING",
                        }
                        for name in (
                            state["ids"] if second_page else state["ids"][:1]
                        )
                    ],
                }
                if not second_page:
                    response["next_page_token"] = "next+page"
            elif target.path == "/v1/runs/run-wire":
                response = {
                    "run": {
                        "status": "COMPLETED"
                        if state["completed"] == set(state["ids"])
                        else "RUNNING"
                    }
                }
            elif target.path.endswith("/connect"):
                simulation_id = target.path.split("/")[-2]
                response = {
                    "status": "READY",
                    "wss_url": f"{state['relay_url']}/{simulation_id}",
                    "token": f"synthetic-{simulation_id}",
                    "expires_at": time.time() + 60,
                }
                response.update(state["offer_overrides"])
            else:
                status, response = 404, {"error": "unknown resource"}
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        def log_message(self, *args):
            pass

    hosted = http_server.HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=hosted.serve_forever)
    worker.start()
    try:
        yield f"http://127.0.0.1:{hosted.server_port}/v1", state
    finally:
        hosted.shutdown()
        worker.join(timeout=2)
        hosted.server_close()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["connect", "run"])
async def test_public_client_completes_authenticated_wire_journey(
    monkeypatch, api_endpoint, tls_contexts, caplog, capsys, entry
):
    url, state = api_endpoint
    serving, trusting = tls_contexts
    upgrades, received, closed = [], {}, set()
    seen, cleaned, connections, transports = {}, [], [], []

    def authenticate(connection, request):
        simulation_id = request.path.removeprefix("/sessions/")
        authorization = request.headers.get("Authorization")
        upgrades.append((request.path, authorization))
        if authorization != f"Bearer synthetic-{simulation_id}":
            return connection.respond(401, "invalid credential")
        return None

    async def relay(socket):
        simulation_id = socket.request.path.removeprefix("/sessions/")
        try:
            await socket.send(READY)
            greeting = await socket.recv()
            await socket.send(CALLER_AUDIO[simulation_id])
            acknowledgement = await socket.recv()
            reply = await socket.recv()
            received[simulation_id] = (greeting, acknowledgement, reply)
            state["completed"].add(simulation_id)
            await socket.send('{"type":"session_end"}')
            await socket.wait_closed()
        finally:
            closed.add(simulation_id)

    async def application(connection):
        connections.append(connection)
        assert connection.result is None
        seen[connection.simulation_id] = []
        try:
            assert await connection.send_audio(GREETING)
            async for audio in connection.receive_audio():
                seen[connection.simulation_id].append(audio)
                assert await connection.send_audio(REPLY)
        finally:
            cleaned.append(connection.simulation_id)

    # Trust the ephemeral CA; keep discovery and the TLS upgrade real.
    connect = _connection.client.connect

    async def connect_with_test_ca(*args, **kwargs):
        transport = await connect(*args, ssl=trusting, **kwargs)
        transports.append(transport)
        return transport

    monkeypatch.setattr(_connection.client, "connect", connect_with_test_ca)
    monkeypatch.setattr(_defaults, "POLL_INTERVAL_SECONDS", 0.001)
    async with websocket_server.serve(
        relay,
        "127.0.0.1",
        0,
        ssl=serving,
        process_request=authenticate,
        close_timeout=0.2,
    ) as hosted:
        state["relay_url"] = (
            f"wss://127.0.0.1:{hosted.sockets[0].getsockname()[1]}/sessions"
        )
        client = coval_agent_sdk.CovalVoiceClient(
            api_key="synthetic-key",
            api_base_url=url,
            workspace_id="workspace-wire",
        )
        if entry == "run":
            result = await asyncio.wait_for(
                client.run(
                    coval_agent_sdk.RunConfig(
                        "agent-wire",
                        "persona-wire",
                        "set-wire",
                        concurrency=2,
                        run_timeout_seconds=4,
                    ),
                    on_connection=application,
                ),
                5,
            )
            expected_ids = IDS
            assert result == coval_agent_sdk.RunResult(
                "run-wire",
                "COMPLETED",
                tuple(
                    coval_agent_sdk.ConversationResult(name, 640, 1280)
                    for name in IDS
                ),
            )
        else:

            async def direct():
                async with client.connect(IDS[0]) as connection:
                    await application(connection)
                return connection.result

            result = await asyncio.wait_for(direct(), 5)
            expected_ids = IDS[:1]
            assert result == coval_agent_sdk.ConversationResult(
                IDS[0], 640, 1280
            )
        assert all(connection.closed for connection in connections)
        assert len(transports) == len(expected_ids)
        assert all(
            socket.state is protocol.State.CLOSED for socket in transports
        )
    assert closed == set(expected_ids)
    assert sorted(cleaned) == sorted(expected_ids)
    assert len(connections) == len(expected_ids)
    assert seen == {name: [CALLER_AUDIO[name]] for name in expected_ids}
    assert sorted(upgrades) == sorted(
        (f"/sessions/{name}", f"Bearer synthetic-{name}")
        for name in expected_ids
    )
    for name in expected_ids:
        greeting, acknowledgement, reply = received[name]
        assert greeting == GREETING
        assert json.loads(acknowledgement) == {
            "type": "audio_ack",
            "received_bytes": 640,
        }
        assert reply == REPLY
    launches = [
        request for request in state["requests"] if request[0] == "POST"
    ]
    if entry == "connect":
        assert launches == []
    else:
        assert len(launches) == 1
        assert launches[0][1] == "/v1/runs"
        assert json.loads(launches[0][4]) == {
            "agent_id": "agent-wire",
            "persona_id": "persona-wire",
            "test_set_id": "set-wire",
            "options": {"iteration_count": 1, "concurrency": 2},
        }
        pages = [r[2] for r in state["requests"] if r[1] == "/v1/simulations"]
        assert pages[:2] == [
            {"filter": ['run_id="run-wire"'], "page_size": ["100"]},
            {
                "filter": ['run_id="run-wire"'],
                "page_size": ["100"],
                "page_token": ["next+page"],
            },
        ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        connections[0].result.sent_audio_bytes = 0
    assert capsys.readouterr() == ("", "")
    assert caplog.records == []


@pytest.mark.asyncio
async def test_public_batch_keeps_tls_peers_and_queued_session_after_1013(
    monkeypatch, api_endpoint, tls_contexts
):
    url, state = api_endpoint
    serving, trusting = tls_contexts
    bad, healthy, queued = (*IDS, "conversation-c")
    state["ids"] = (bad, healthy, queued)
    state["queued_until"] = {queued: bad}
    healthy_started, queued_started = asyncio.Event(), asyncio.Event()
    closed, cleaned = set(), set()

    async def relay(socket):
        name = socket.request.path.removeprefix("/sessions/")
        assert (
            socket.request.headers["Authorization"]
            == f"Bearer synthetic-{name}"
        )
        try:
            await socket.send(READY)
            if name == bad:
                await healthy_started.wait()
                state["completed"].add(name)
                await socket.close(1013, "synthetic overload")
                return
            if name == healthy:
                healthy_started.set()
                await queued_started.wait()
            else:
                queued_started.set()
            await socket.send(REPLY)
            assert json.loads(await socket.recv()) == {
                "type": "audio_ack",
                "received_bytes": len(REPLY),
            }
            assert await socket.recv() == REPLY
            state["completed"].add(name)
            await socket.send('{"type":"session_end"}')
            await socket.wait_closed()
        finally:
            closed.add(name)

    async def application(connection):
        try:
            async for audio in connection.receive_audio():
                await connection.send_audio(audio)
        finally:
            cleaned.add(connection.simulation_id)

    connect = _connection.client.connect

    async def connect_with_ca(*args, **kwargs):
        return await connect(*args, ssl=trusting, **kwargs)

    monkeypatch.setattr(_connection.client, "connect", connect_with_ca)
    monkeypatch.setattr(_defaults, "POLL_INTERVAL_SECONDS", 0.001)
    async with websocket_server.serve(
        relay, "127.0.0.1", 0, ssl=serving, close_timeout=0.2
    ) as hosted:
        state["relay_url"] = (
            f"wss://127.0.0.1:{hosted.sockets[0].getsockname()[1]}/sessions"
        )
        client = coval_agent_sdk.CovalVoiceClient(
            api_key="synthetic-key",
            api_base_url=url,
            workspace_id="workspace-wire",
        )
        with pytest.raises(coval_agent_sdk.RunError) as raised:
            await asyncio.wait_for(
                client.run(
                    coval_agent_sdk.RunConfig(
                        "agent-wire",
                        "persona-wire",
                        "set-wire",
                        concurrency=2,
                        run_timeout_seconds=4,
                    ),
                    application,
                ),
                5,
            )
    result = raised.value.result
    assert result.status == "COMPLETED"
    assert {c.simulation_id for c in result.conversations} == {healthy, queued}
    assert result.failures == (
        coval_agent_sdk.ConversationFailure(
            bad,
            "CovalError",
            f"Simulation {bad} relay closed without completion (code 1013).",
        ),
    )
    assert closed == cleaned == set(state["ids"])
    assert len([r for r in state["requests"] if r[0] == "POST"]) == 1
    assert not any(r[1] == f"/v1/simulations/{bad}" for r in state["requests"])


@pytest.mark.parametrize(
    "construct",
    [
        lambda: coval_agent_sdk.CovalVoiceClient("synthetic-key"),
        lambda: coval_agent_sdk.CovalVoiceClient(
            api_key="synthetic-key", unknown=True
        ),
        lambda: coval_agent_sdk.RunConfig(
            agent_id="agent", persona_id="persona"
        ),
        lambda: coval_agent_sdk.RunConfig(
            "agent", "persona", "set", unknown=True
        ),
        lambda: coval_agent_sdk.ConversationResult(
            simulation_id="conversation", received_audio_bytes=2
        ),
    ],
)
def test_public_constructors_reject_invalid_argument_binding(construct):
    with pytest.raises(TypeError):
        construct()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "diagnostic"),
    [
        ({"wss_url": "ws://127.0.0.1/"}, "without a WSS URL"),
        ({"token": ""}, "without a token"),
        ({"expires_at": 0}, "expired connection offer"),
        ({"expires_at": "tomorrow"}, "expired connection offer"),
        ({"status": "UNKNOWN"}, "unknown connection status"),
    ],
)
async def test_public_connect_rejects_bad_offers_before_upgrade(
    monkeypatch, api_endpoint, override, diagnostic
):
    url, state = api_endpoint
    state["relay_url"] = "wss://127.0.0.1:1/sessions"
    state["offer_overrides"] = override
    upgrade = mock.AsyncMock()
    monkeypatch.setattr(_connection.client, "connect", upgrade)
    voice = coval_agent_sdk.CovalVoiceClient(
        api_key="synthetic-key",
        api_base_url=url,
        workspace_id="workspace-wire",
    )
    with pytest.raises(coval_agent_sdk.CovalError, match=diagnostic):
        async with voice.connect(IDS[0]):
            pytest.fail("A malformed offer reached application code")
    upgrade.assert_not_called()
    assert [r[:2] for r in state["requests"]] == [
        ("GET", f"/v1/simulations/{IDS[0]}/connect")
    ]
