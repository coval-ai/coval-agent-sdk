"""Public audio connections with independent application-owned pipelines."""

import asyncio
import json
import types
from unittest import mock

import pytest
from websockets import exceptions
from websockets import frames
from websockets.asyncio import client as websocket_client
from websockets.asyncio import server

import coval_agent_sdk
from coval_agent_sdk import _audio
from coval_agent_sdk import _connection
from coval_agent_sdk import _defaults

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
END = '{"type":"session_end"}'
PCM = b"\x01\x00" * 320
REPLY = b"\x02\x00" * 320


class Socket:
    def __init__(self, *messages):
        self.messages = asyncio.Queue()
        for message in messages:
            self.messages.put_nowait(message)
        self.sent = []
        self.closed = False
        self.audio_sent = asyncio.Event()
        self.reading = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.reading.set()
        message = await self.messages.get()
        if message is None:
            raise StopAsyncIteration
        if isinstance(message, BaseException):
            raise message
        return message

    async def send(self, message):
        self.sent.append(message)
        if isinstance(message, bytes):
            self.audio_sent.set()


def setup_connection(monkeypatch, socket, **kwargs):
    monkeypatch.setattr(
        _connection,
        "open_voice_connection",
        mock.AsyncMock(return_value=socket),
    )
    return coval_agent_sdk.CovalVoiceClient(api_key="synthetic-key", **kwargs)


@pytest.mark.asyncio
async def test_real_websocket_greeting_input_ack_reply_and_final_result(
    monkeypatch,
):
    observed = []

    async def relay(socket):
        await socket.send(READY)
        observed.append(await socket.recv())
        await socket.send(PCM)
        observed.append(await socket.recv())
        observed.append(await socket.recv())
        await socket.send(END)

    async with server.serve(relay, "127.0.0.1", 0) as hosted:
        port = hosted.sockets[0].getsockname()[1]

        async def open_local(api, simulation_id):
            return await websocket_client.connect(f"ws://127.0.0.1:{port}")

        monkeypatch.setattr(_connection, "open_voice_connection", open_local)
        voice = coval_agent_sdk.CovalVoiceClient(api_key="synthetic-key")
        async with voice.connect("conversation-a") as connection:
            assert connection.simulation_id == "conversation-a"
            assert connection.result is None
            assert await connection.send_audio(REPLY)
            async for audio in connection.receive_audio():
                assert audio == PCM
                assert await connection.send_audio(audio)
        assert connection.closed
        assert connection.result == coval_agent_sdk.SessionResult(
            "conversation-a", 640, 1280
        )
        assert not await connection.send_audio(PCM)
    assert observed == [
        REPLY,
        '{"type": "audio_ack", "received_bytes": 640}',
        PCM,
    ]


@pytest.mark.asyncio
async def test_context_waits_for_readiness_before_application_code(monkeypatch):
    socket = Socket()
    voice = setup_connection(monkeypatch, socket)
    entered = asyncio.Event()

    async def connect():
        async with voice.connect("conversation-a") as connection:
            entered.set()
            await connection.wait_closed()

    task = asyncio.create_task(connect())
    await socket.reading.wait()
    assert not entered.is_set()
    socket.messages.put_nowait(READY)
    await asyncio.wait_for(entered.wait(), 1)
    socket.messages.put_nowait(END)
    await asyncio.wait_for(task, 1)
    assert socket.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("messages", "diagnostic"),
    [
        (["{"], "invalid control JSON"),
        (["[]"], "invalid control JSON"),
        ([END], "before session_ready"),
        ([PCM], "before session_ready"),
        ([READY, READY], "repeated session_ready"),
        ([READY, b"x"], "even bytes"),
        ([READY.replace("16000", "24000")], "unsupported audio"),
        (
            [READY, '{"type":"error","message":"synthetic failure"}'],
            "synthetic failure",
        ),
    ],
)
async def test_protocol_errors_fail_and_close_transport(
    monkeypatch, messages, diagnostic
):
    socket = Socket(*messages)
    exporter = mock.Mock()
    voice = setup_connection(monkeypatch, socket, trace_exporter=exporter)
    with pytest.raises(coval_agent_sdk.CovalError, match=diagnostic):
        async with voice.connect("conversation-a") as connection:
            await connection.wait_closed()
    assert socket.closed
    exporter.upload.assert_not_called()
    exporter.discard.assert_called_once_with(simulation_id="conversation-a")


@pytest.mark.asyncio
async def test_missing_readiness_is_bounded_and_cancellable(monkeypatch):
    socket = Socket()
    voice = setup_connection(monkeypatch, socket)
    monkeypatch.setattr(_defaults, "CONNECT_WAIT_SECONDS", 0.01)
    with pytest.raises(
        coval_agent_sdk.CovalError, match="did not send session_ready"
    ):
        async with voice.connect("conversation-a"):
            pytest.fail("Unready connection reached application code")
    assert socket.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "audio", ["xx", [0, 0], b"x", bytes(2 * 1024 * 1024 + 2)]
)
async def test_output_rejected_before_any_wire_audio(monkeypatch, audio):
    socket = Socket(READY)
    voice = setup_connection(monkeypatch, socket)
    with pytest.raises(coval_agent_sdk.CovalError, match=r"PCM16|2 MiB"):
        async with voice.connect("conversation-a") as connection:
            await connection.send_audio(audio)
    assert not socket.sent
    assert socket.closed


@pytest.mark.asyncio
async def test_adjacent_and_concurrent_sends_preserve_pacing_and_byte_counts(
    monkeypatch,
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

    class TimedSocket(Socket):
        async def send(self, message):
            if isinstance(message, bytes):
                sent_at.append(clock[0])
            await super().send(message)

    socket = TimedSocket(READY)
    voice = setup_connection(monkeypatch, socket)
    async with voice.connect("conversation-a") as connection:
        assert await connection.send_audio(b"")
        assert await connection.send_audio(PCM)
        assert all(
            await asyncio.gather(
                connection.send_audio(REPLY), connection.send_audio(PCM)
            )
        )
        socket.messages.put_nowait(END)
        await connection.wait_closed()
    assert [m for m in socket.sent if isinstance(m, bytes)] == [PCM, REPLY, PCM]
    assert sent_at == pytest.approx([10, 10.02, 10.04])
    assert connection.result.sent_audio_bytes == 1920


@pytest.mark.asyncio
async def test_input_is_acknowledged_during_a_blocked_output_write(monkeypatch):
    blocked = asyncio.Event()
    sender_stopped = asyncio.Event()

    class HeldSocket(Socket):
        async def send(self, message):
            if isinstance(message, bytes):
                blocked.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    sender_stopped.set()
            else:
                await super().send(message)

    socket = HeldSocket(READY)
    voice = setup_connection(monkeypatch, socket)
    async with voice.connect("conversation-a") as connection:
        writing = asyncio.create_task(connection.send_audio(PCM))
        await blocked.wait()
        socket.messages.put_nowait(PCM)
        socket.messages.put_nowait(PCM)
        stream = connection.receive_audio()
        assert await anext(stream) == PCM
        assert await anext(stream) == PCM
        await stream.aclose()
        assert [json.loads(m)["received_bytes"] for m in socket.sent] == [
            640,
            1280,
        ]
        socket.messages.put_nowait(END)
        assert await writing is False
    assert sender_stopped.is_set()
    assert connection.result.sent_audio_bytes == 0


@pytest.mark.asyncio
async def test_receive_budget_counts_bytes_and_preserves_boundary(monkeypatch):
    limit = 64 * 1024
    socket = Socket(READY, bytes(limit), END)
    voice = setup_connection(monkeypatch, socket)
    async with voice.connect("boundary") as connection:
        assert [audio async for audio in connection.receive_audio()] == [
            bytes(limit)
        ]
    assert connection.result.received_audio_bytes == limit
    socket = Socket(READY, bytes(limit), b"\x01\x00", END)
    voice = setup_connection(monkeypatch, socket)
    with pytest.raises(
        coval_agent_sdk.CovalError, match="64 KiB receive buffer"
    ):
        async with voice.connect("overflow") as connection:
            await connection.wait_closed()
    assert socket.sent == [
        json.dumps({"type": "audio_ack", "received_bytes": limit})
    ]
    assert socket.closed


@pytest.mark.asyncio
async def test_only_one_receiver_and_early_exit_never_reports_success(
    monkeypatch,
):
    socket = Socket(READY, PCM)
    voice = setup_connection(monkeypatch, socket)
    with pytest.raises(  # noqa: PT012 - Exercise caller code inside the context.
        coval_agent_sdk.CovalError, match="before the conversation completed"
    ):
        async with voice.connect("conversation-a") as connection:
            first = connection.receive_audio()
            assert await anext(first) == PCM
            second = connection.receive_audio()
            with pytest.raises(RuntimeError, match="Only one"):
                await anext(second)
            await first.aclose()
    assert connection.result is None
    assert socket.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "PENDING",
        "MALFORMED",
        "UNREACHABLE",
        "UNAUTHORIZED",
        "INVALID_JSON",
    ],
)
@pytest.mark.parametrize("abnormal", [False, True])
async def test_lost_terminal_frame_requires_authoritative_completion(
    monkeypatch, status, abnormal
):
    socket = Socket(READY)
    voice = setup_connection(monkeypatch, socket)
    read = mock.AsyncMock(
        return_value={}
        if status == "MALFORMED"
        else {"simulation": {"status": status}}
    )
    if status == "UNREACHABLE":
        read.side_effect = coval_agent_sdk.CovalConnectionError("offline")
    elif status == "UNAUTHORIZED":
        read.side_effect = coval_agent_sdk.CovalHttpError(
            status_code=401, message="denied"
        )
    elif status == "INVALID_JSON":
        read.side_effect = coval_agent_sdk.TunnelError("invalid JSON")
    monkeypatch.setattr(voice._api, "get", read)
    monkeypatch.setattr(_defaults, "CLOSE_CONFIRM_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(_defaults, "POLL_INTERVAL_SECONDS", 0)

    async def exchange():
        async with voice.connect("conversation-a") as connection:
            socket.messages.put_nowait(
                exceptions.ConnectionClosedError(None, None)
                if abnormal
                else None
            )
            await connection.wait_closed()
        return connection.result

    if status == "COMPLETED":
        assert (await asyncio.wait_for(exchange(), 1)).status == "COMPLETED"
    else:
        with pytest.raises(coval_agent_sdk.CovalError):
            await asyncio.wait_for(exchange(), 1)
    assert read.await_count >= 1
    assert socket.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [1008, 1013])
async def test_explicit_relay_rejection_never_falls_back_to_success(
    monkeypatch, code
):
    socket = Socket(READY)
    voice = setup_connection(monkeypatch, socket)
    read = mock.AsyncMock(return_value={"simulation": {"status": "COMPLETED"}})
    monkeypatch.setattr(voice._api, "get", read)
    with pytest.raises(coval_agent_sdk.CovalError):  # noqa: PT012 - Inject a relay failure after context entry.
        async with voice.connect("conversation-a") as connection:
            socket.messages.put_nowait(
                exceptions.ConnectionClosedError(
                    frames.Close(code, "rejected"), None
                )
            )
            await connection.wait_closed()
    read.assert_not_awaited()
    assert socket.closed


@pytest.mark.asyncio
async def test_failure_before_readiness_never_queries_for_completion(
    monkeypatch,
):
    socket = Socket(exceptions.ConnectionClosedError(None, None))
    voice = setup_connection(monkeypatch, socket)
    read = mock.AsyncMock()
    monkeypatch.setattr(voice._api, "get", read)
    with pytest.raises(coval_agent_sdk.CovalError):
        async with voice.connect("conversation-a"):
            pytest.fail("Closed relay was yielded")
    read.assert_not_awaited()
    assert socket.closed


@pytest.mark.asyncio
async def test_cancellation_during_completion_confirmation_closes_transport(
    monkeypatch,
):
    socket = Socket(READY)
    voice = setup_connection(monkeypatch, socket)
    verifying = asyncio.Event()

    async def read(path):
        verifying.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(voice._api, "get", read)

    async def exchange():
        async with voice.connect("conversation-a") as connection:
            socket.messages.put_nowait(None)
            await connection.wait_closed()

    task = asyncio.create_task(exchange())
    await verifying.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert socket.closed


@pytest.mark.asyncio
async def test_failed_write_wakes_other_connection_observers(monkeypatch):
    original = OSError("synthetic send failure")

    class BrokenSocket(Socket):
        async def send(self, message):
            if isinstance(message, bytes):
                raise original
            await super().send(message)

    socket = BrokenSocket(READY)
    voice = setup_connection(monkeypatch, socket)
    with pytest.raises(coval_agent_sdk.CovalConnectionError) as caught:  # noqa: PT012 - Compare the send and observer failure identities.
        async with voice.connect("conversation-a") as connection:
            observing = asyncio.create_task(connection.wait_closed())
            try:
                await connection.send_audio(PCM)
            finally:
                with pytest.raises(
                    coval_agent_sdk.CovalConnectionError
                ) as observer:
                    await observing
    assert caught.value is observer.value
    assert caught.value.__cause__ is original
    assert connection.result is None
    assert socket.closed


@pytest.mark.asyncio
async def test_terminal_frame_stops_output_before_socket_shutdown(monkeypatch):
    closing = asyncio.Event()
    allow_close = asyncio.Event()

    class ClosingSocket(Socket):
        async def __aexit__(self, *args):
            closing.set()
            await allow_close.wait()
            await super().__aexit__(*args)

    socket = ClosingSocket(READY)
    voice = setup_connection(monkeypatch, socket)

    async def exchange():
        async with voice.connect("conversation-a") as connection:
            socket.messages.put_nowait(END)
            await closing.wait()
            await connection.wait_closed()
            assert not await connection.send_audio(PCM)
            assert not socket.closed
            allow_close.set()
        assert connection.result.status == "COMPLETED"

    await asyncio.wait_for(exchange(), 1)
    assert socket.closed


def test_public_package_imports_its_own_tracing_module(tmp_path):
    import subprocess
    import sys

    code = """
import sys
import types
sys.modules['tracing'] = types.ModuleType('consumer_owned_tracing')
from coval_agent_sdk import tracing
assert tracing.CovalTraceExporter.__module__ == 'coval_agent_sdk.tracing'
"""
    subprocess.run(  # noqa: S603 - Fixed interpreter and source, no user input.
        [sys.executable, "-I", "-c", code], cwd=tmp_path, check=True
    )
