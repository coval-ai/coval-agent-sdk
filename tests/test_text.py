"""Text connections over loopback HTTP discovery and TLS WebSockets."""

import asyncio
import json

import pytest
from websockets.asyncio import server as websocket_server

import coval_agent_sdk
from coval_agent_sdk import _connection
from coval_agent_sdk import _defaults
from coval_agent_sdk import _text

IDS = ("conversation-a", "conversation-b")
TEXT_READY = json.dumps({"type": "session_ready", "modality": "text"})
VOICE_READY = json.dumps(
    {
        "type": "session_ready",
        "audio": {
            "encoding": "pcm_s16le",
            "sample_rate_hz": 16000,
            "channels": 1,
        },
    }
)
END = '{"type":"session_end"}'


def _trust(monkeypatch, trusting):
    connect = _connection.client.connect

    async def connect_with_test_ca(*args, **kwargs):
        return await connect(*args, ssl=trusting, **kwargs)

    monkeypatch.setattr(_connection.client, "connect", connect_with_test_ca)
    monkeypatch.setattr(_defaults, "POLL_INTERVAL_SECONDS", 0.001)


def _client(url):
    return coval_agent_sdk.CovalTextClient(
        api_key="synthetic-key", api_base_url=url, workspace_id="workspace-wire"
    )


async def _serve(relay, serving, state):
    hosted = await websocket_server.serve(
        relay, "127.0.0.1", 0, ssl=serving, close_timeout=0.2
    )
    port = hosted.sockets[0].getsockname()[1]
    state["relay_url"] = f"wss://127.0.0.1:{port}/sessions"
    return hosted


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["connect", "run"])
async def test_text_client_exchanges_turns_until_session_end(
    monkeypatch, api_endpoint, tls_contexts, caplog, capsys, entry
):
    url, state = api_endpoint
    serving, trusting = tls_contexts
    replies: dict[str, list[dict]] = {}

    async def relay(socket):
        simulation_id = socket.request.path.removeprefix("/sessions/")
        await socket.send(TEXT_READY)
        replies[simulation_id] = []
        for turn in ("Hi, I need help.", "Thanks!"):
            await socket.send(json.dumps({"type": "message", "text": turn}))
            replies[simulation_id].append(json.loads(await socket.recv()))
        state["completed"].add(simulation_id)
        await socket.send(END)
        await socket.wait_closed()

    async def application(connection):
        async for message in connection.receive_messages():
            assert await connection.send_message(f"Echo: {message}")

    _trust(monkeypatch, trusting)
    hosted = await _serve(relay, serving, state)
    try:
        client = _client(url)
        if entry == "run":
            result = await asyncio.wait_for(
                client.run(
                    coval_agent_sdk.RunConfig(
                        "agent", "persona", "set", concurrency=2
                    ),
                    on_connection=application,
                ),
                5,
            )
            expected_ids = IDS
            conversations = result.conversations
            assert result.status == "COMPLETED"
        else:

            async def direct():
                async with client.connect(IDS[0]) as connection:
                    await application(connection)
                return connection.result

            conversations = (await asyncio.wait_for(direct(), 5),)
            expected_ids = IDS[:1]
    finally:
        hosted.close()
        await hosted.wait_closed()

    assert conversations == tuple(
        coval_agent_sdk.ConversationResult(
            name, 0, 0, received_messages=2, sent_messages=2
        )
        for name in expected_ids
    )
    assert replies == {
        name: [
            {"type": "message", "text": "Echo: Hi, I need help."},
            {"type": "message", "text": "Echo: Thanks!"},
        ]
        for name in expected_ids
    }
    assert capsys.readouterr() == ("", "")
    assert caplog.records == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ready", "client_type", "message"),
    [
        (
            TEXT_READY,
            "voice",
            "uses a text agent; connect with CovalTextClient",
        ),
        (
            VOICE_READY,
            "text",
            "uses a voice agent; connect with CovalVoiceClient",
        ),
    ],
)
async def test_mismatched_modality_fails_before_the_handler_runs(
    monkeypatch, api_endpoint, tls_contexts, ready, client_type, message
):
    url, state = api_endpoint
    serving, trusting = tls_contexts

    async def relay(socket):
        await socket.send(ready)
        await socket.wait_closed()

    _trust(monkeypatch, trusting)
    hosted = await _serve(relay, serving, state)
    client = (
        _client(url)
        if client_type == "text"
        else coval_agent_sdk.CovalVoiceClient(
            api_key="synthetic-key",
            api_base_url=url,
            workspace_id="workspace-wire",
        )
    )
    entered = False
    try:
        with pytest.raises(coval_agent_sdk.CovalError, match=message):
            async with client.connect(IDS[0]):
                entered = True
    finally:
        hosted.close()
        await hosted.wait_closed()
    assert entered is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("frame", "message"),
    [
        (b"\x01\x00", "Relay sent audio to a text session"),
        (json.dumps({"type": "message"}), "message without text"),
    ],
)
async def test_text_protocol_violations_fail_the_conversation(
    monkeypatch, api_endpoint, tls_contexts, frame, message
):
    url, state = api_endpoint
    serving, trusting = tls_contexts

    async def relay(socket):
        await socket.send(TEXT_READY)
        await socket.send(frame)
        await socket.wait_closed()

    _trust(monkeypatch, trusting)
    hosted = await _serve(relay, serving, state)
    try:

        async def consume():
            async with _client(url).connect(IDS[0]) as connection:
                async for _ in connection.receive_messages():
                    pass

        with pytest.raises(coval_agent_sdk.CovalError, match=message):
            await consume()
    finally:
        hosted.close()
        await hosted.wait_closed()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "nonempty string"),
        ("   ", "nonempty string"),
        (b"bytes", "nonempty string"),
        ("x" * (_defaults.MAX_TEXT_MESSAGE_BYTES + 1), "64 KiB"),
    ],
)
def test_invalid_replies_are_rejected_before_any_write(text, message):
    with pytest.raises(coval_agent_sdk.CovalError, match=message):
        _text.encode_message(text)


@pytest.mark.asyncio
async def test_send_before_readiness_and_unread_overflow_fail():
    connection = coval_agent_sdk.TextConnection("conversation-a")
    with pytest.raises(coval_agent_sdk.CovalError, match="not ready"):
        await connection.send_message("Hello")
    state = connection._state
    for index in range(_defaults.MAX_UNREAD_TEXT_MESSAGES):
        state.feed_message({"type": "message", "text": str(index)})
    with pytest.raises(coval_agent_sdk.CovalError, match="64 unread"):
        state.feed_message({"type": "message", "text": "overflow"})
