"""Focused lifecycle tests for the importable voice client."""

import asyncio
import contextlib
import itertools
import json
import time
import unittest
from unittest import mock
from urllib import error

from websockets import exceptions
from websockets import frames

from coval_agent_sdk import _audio
from coval_agent_sdk import _connection
from coval_agent_sdk import _defaults
from coval_agent_sdk import _http
from coval_agent_sdk import _runs
from coval_agent_sdk import _session
from coval_agent_sdk import client as voice_client
from coval_agent_sdk import config as run_config
from coval_agent_sdk import errors
from coval_agent_sdk import tracing

TOKEN_EXPIRES_AT_EPOCH_SECONDS = 9_999_999_999
SESSION_READY = (
    '{"type":"session_ready","audio":{"encoding":"pcm_s16le",'
    '"sample_rate_hz":16000,"channels":1}}'
)


class SocketContext:
    """Own the socket context across scripted receive scenarios."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        return None


class FakeApi:
    """Return two simulations for a run that remains active."""

    async def post(self, path: str, payload: dict) -> dict:
        return {"run": {"run_id": "run-1"}}

    async def get(self, path: str, *, query: dict | None = None) -> dict:
        if path == "simulations":
            return {
                "simulations": [
                    {"simulation_id": "broken", "status": "RUNNING"},
                    {"simulation_id": "waiting", "status": "RUNNING"},
                ]
            }
        await asyncio.sleep(0)
        return {"run": {"status": "RUNNING"}}


class ClientLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_session_ready_times_out_and_closes_agent(
        self,
    ) -> None:
        agent = mock.Mock(close=mock.AsyncMock())
        cancelled_receive = asyncio.Event()

        class SilentSocket(SocketContext):
            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled_receive.set()

        with (
            mock.patch.object(
                _connection,
                "open_voice_connection",
                new=mock.AsyncMock(return_value=SilentSocket()),
            ),
            mock.patch.object(_defaults, "CONNECT_WAIT_SECONDS", 0.01),
            self.assertRaisesRegex(
                errors.TunnelError, "did not send session_ready"
            ),
        ):
            await _session.run_session(
                mock.Mock(), "simulation-1", lambda _id: agent
            )
        self.assertTrue(cancelled_receive.is_set())
        agent.close.assert_awaited_once()

    async def test_batch_deadline_cancels_sessions_only_on_expiry(
        self,
    ) -> None:
        for timeout_seconds, elapsed_seconds, expires in (
            (1800, 1000, False),
            (10, 20, True),
        ):
            with self.subTest(timeout_seconds=timeout_seconds):
                now = 0
                cancelled = False

                class BatchApi(FakeApi):
                    async def get(
                        self, path: str, *, query: dict | None = None
                    ) -> dict:
                        nonlocal now
                        await asyncio.sleep(0)
                        if path == "simulations":
                            return {
                                "simulations": [
                                    {
                                        "simulation_id": "attached",
                                        "status": "RUNNING",
                                    }
                                ]
                            }
                        now = elapsed_seconds  # noqa: B023 - This subtest completes before the next case.
                        return {
                            "run": {
                                "status": "RUNNING" if expires else "COMPLETED"  # noqa: B023 - Each case is awaited.
                            }
                        }

                async def session(*args) -> None:
                    nonlocal cancelled
                    if expires:  # noqa: B023 - The session is awaited within this case.
                        try:
                            await asyncio.Event().wait()
                        except asyncio.CancelledError:
                            cancelled = True
                            raise

                config = run_config.RunConfig(
                    agent_id="agent",
                    persona_id="persona",
                    test_set_id="tests",
                    run_timeout_seconds=timeout_seconds,
                )
                with (
                    mock.patch.object(
                        _runs,
                        "time",
                        mock.Mock(monotonic=lambda: now),  # noqa: B023 - Each case is awaited.
                    ),
                    mock.patch.object(_session, "run_session", session),
                    mock.patch.object(_defaults, "POLL_INTERVAL_SECONDS", 0),
                ):
                    if expires:
                        with self.assertRaisesRegex(
                            errors.TunnelError, "within 10 seconds"
                        ):
                            await _runs.launch_and_run(
                                BatchApi(), config, mock.Mock()
                            )
                    else:
                        await _runs.launch_and_run(
                            BatchApi(), config, mock.Mock()
                        )
                self.assertEqual(cancelled, expires)

    async def test_monitoring_recovers_without_canceling_live_sessions(
        self,
    ) -> None:
        session_finished = asyncio.Event()
        session_cancelled = False

        class MonitoringApi(FakeApi):
            listing_calls = 0
            status_calls = 0

            async def get(
                self, path: str, *, query: dict | None = None
            ) -> dict:
                if path == "simulations":
                    self.listing_calls += 1
                    if self.listing_calls == 2:
                        raise errors.CovalHttpError(
                            status_code=503, message="temporary listing failure"
                        )
                    return {
                        "simulations": [
                            {"simulation_id": "attached", "status": "RUNNING"}
                        ]
                    }
                self.status_calls += 1
                if self.status_calls == 1:
                    return {"run": {"status": "RUNNING"}}
                if self.status_calls == 2:
                    raise errors.CovalConnectionError(
                        "temporary status failure"
                    )
                session_finished.set()
                return {"run": {"status": "COMPLETED"}}

        async def session(*args) -> None:
            nonlocal session_cancelled
            try:
                await session_finished.wait()
            except asyncio.CancelledError:
                session_cancelled = True
                raise

        api = MonitoringApi()
        config = run_config.RunConfig(
            agent_id="agent", persona_id="persona", test_set_id="tests"
        )
        with (
            mock.patch.object(_session, "run_session", session),
            mock.patch.object(_defaults, "POLL_INTERVAL_SECONDS", 0),
        ):
            await asyncio.wait_for(
                _runs.launch_and_run(api, config, mock.Mock()), timeout=1
            )
        self.assertTrue(session_finished.is_set())
        self.assertFalse(session_cancelled)

    async def test_monitoring_retries_only_temporary_http_and_network_errors(
        self,
    ) -> None:
        failures = [
            errors.CovalHttpError(status_code=status, message="temporary")
            for status in _defaults.RETRYABLE_HTTP_STATUSES
        ]
        failures += [
            errors.CovalConnectionError("unreachable"),
            error.URLError("unreachable"),
        ]
        for failure in failures:
            with self.subTest(failure=failure):
                api = mock.Mock()
                api.get = mock.AsyncMock(
                    side_effect=[failure, {"run": {"status": "COMPLETED"}}]
                )
                with mock.patch.object(asyncio, "sleep", new=mock.AsyncMock()):
                    result = await _runs._get_monitoring_resource(
                        api,
                        "runs/run-1",
                        run_deadline=time.monotonic()
                        + _defaults.RUN_WAIT_SECONDS,
                    )
                self.assertEqual(result["run"]["status"], "COMPLETED")
        for status in (401, 403, 404):
            with self.subTest(status=status):
                api = mock.Mock()
                api.get = mock.AsyncMock(
                    side_effect=errors.CovalHttpError(
                        status_code=status, message="denied"
                    )
                )
                with self.assertRaises(errors.CovalHttpError):
                    await _runs._get_monitoring_resource(
                        api,
                        "runs/run-1",
                        run_deadline=time.monotonic()
                        + _defaults.RUN_WAIT_SECONDS,
                    )
                api.get.assert_awaited_once()

    async def test_monitoring_outages_obey_both_retry_and_run_deadlines(
        self,
    ) -> None:
        for remaining_run_seconds in (5, _defaults.RUN_WAIT_SECONDS):
            with self.subTest(remaining_run_seconds=remaining_run_seconds):
                deadline = min(
                    remaining_run_seconds, _defaults.MONITORING_RETRY_SECONDS
                )
                api = mock.Mock()
                api.get = mock.AsyncMock(
                    side_effect=errors.CovalConnectionError("unreachable")
                )
                with (
                    mock.patch.object(
                        _runs,
                        "time",
                        new=mock.Mock(
                            monotonic=mock.Mock(
                                side_effect=[0, 0, 0, deadline, deadline]
                            )
                        ),
                    ),
                    mock.patch.object(asyncio, "sleep", new=mock.AsyncMock()),
                    self.assertRaisesRegex(
                        errors.TunnelError, "retry deadline"
                    ),
                ):
                    await _runs._get_monitoring_resource(
                        api, "runs/run-1", run_deadline=remaining_run_seconds
                    )

    async def test_client_passes_simulation_id_to_factory(
        self,
    ) -> None:
        received_simulation_ids = []
        agent = mock.Mock()
        agent.close = mock.AsyncMock()

        def create_agent(simulation_id: str):
            received_simulation_ids.append(simulation_id)
            return agent

        class EndingSocket(SocketContext):
            async def __aiter__(self):
                yield '{"type":"session_end"}'

        client = voice_client.CovalVoiceClient(api_key="test-key")
        with mock.patch.object(
            _connection,
            "open_voice_connection",
            new=mock.AsyncMock(return_value=EndingSocket()),
        ):
            await client.connect("simulation-1", create_agent)
        self.assertEqual(received_simulation_ids, ["simulation-1"])
        agent.close.assert_awaited_once()

    async def test_stalled_reads_are_bounded_and_cancellable(
        self,
    ) -> None:
        read_started = asyncio.Event()
        read_cancelled = asyncio.Event()

        async def stalled_read(*args, **kwargs):
            read_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                read_cancelled.set()

        api = mock.Mock()
        api.get = stalled_read
        run_deadline = time.monotonic() + _defaults.RUN_WAIT_SECONDS
        short_retry_seconds = 0.01
        with (
            mock.patch.object(
                _defaults, "MONITORING_RETRY_SECONDS", short_retry_seconds
            ),
            self.assertRaisesRegex(errors.TunnelError, "retry deadline"),
        ):
            await _runs._get_monitoring_resource(
                api, "runs/run-1", run_deadline=run_deadline
            )
        self.assertTrue(read_cancelled.is_set())
        read_started.clear()
        read_cancelled.clear()
        monitoring = asyncio.create_task(
            _runs._get_monitoring_resource(
                api, "runs/run-1", run_deadline=run_deadline
            )
        )
        await read_started.wait()
        monitoring.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await monitoring
        self.assertTrue(read_cancelled.is_set())

    async def test_discovery_waits_for_a_replacement_after_an_attempt_expires(
        self,
    ) -> None:
        api = mock.Mock()
        api.get = mock.AsyncMock(
            side_effect=[
                {"status": "EXPIRED"},
                {"status": "PROVISIONING"},
                {
                    "status": "READY",
                    "wss_url": "wss://relay.example.test",
                    "token": "replacement-token",
                    "expires_at": TOKEN_EXPIRES_AT_EPOCH_SECONDS,
                },
            ]
        )
        with mock.patch.object(asyncio, "sleep", new=mock.AsyncMock()):
            offer = await _connection.wait_for_offer(api, "simulation-1")
        self.assertEqual(offer.token, "replacement-token")

    async def test_discovery_stops_when_the_simulation_ends_after_expiration(
        self,
    ) -> None:
        for status in ("CLOSED", "FAILED", "CANCELLED", "CONNECTED"):
            with self.subTest(status=status):
                api = mock.Mock()
                api.get = mock.AsyncMock(
                    side_effect=[{"status": "EXPIRED"}, {"status": status}]
                )
                with (
                    mock.patch.object(asyncio, "sleep", new=mock.AsyncMock()),
                    self.assertRaisesRegex(errors.TunnelError, status),
                ):
                    await _connection.wait_for_offer(api, "simulation-1")

    async def test_discovery_retries_transient_errors_but_not_auth(
        self,
    ) -> None:
        ready = {
            "status": "READY",
            "wss_url": "wss://relay.example.test",
            "token": "test-token",
            "expires_at": TOKEN_EXPIRES_AT_EPOCH_SECONDS,
        }
        for status_code in (409, 429, 500, 502, 503, 504):
            with self.subTest(status_code=status_code):
                api = mock.Mock()
                api.get = mock.AsyncMock(
                    side_effect=[
                        errors.CovalHttpError(
                            status_code=status_code, message="retry"
                        ),
                        ready,
                    ]
                )
                with mock.patch.object(asyncio, "sleep", new=mock.AsyncMock()):
                    offer = await _connection.wait_for_offer(
                        api, "simulation-1"
                    )
                self.assertEqual(offer.token, "test-token")
                self.assertEqual(api.get.await_count, 2)

        for status_code in (401, 403, 404):
            with self.subTest(status_code=status_code):
                api = mock.Mock()
                api.get = mock.AsyncMock(
                    side_effect=errors.CovalHttpError(
                        status_code=status_code, message="denied"
                    )
                )
                with self.assertRaises(errors.CovalHttpError):
                    await _connection.wait_for_offer(api, "simulation-1")
                api.get.assert_awaited_once()

    async def test_discovery_retries_do_not_reset_the_connection_deadline(
        self,
    ) -> None:
        for failure in (
            {"status": "EXPIRED"},
            errors.CovalHttpError(status_code=503, message="unavailable"),
            errors.CovalConnectionError("unreachable"),
        ):
            with self.subTest(failure=type(failure).__name__):
                api = mock.Mock()
                api.get = (
                    mock.AsyncMock(return_value=failure)
                    if isinstance(failure, dict)
                    else mock.AsyncMock(side_effect=failure)
                )
                with (
                    mock.patch.object(
                        time,
                        "monotonic",
                        side_effect=[0, 0, _defaults.CONNECT_WAIT_SECONDS],
                    ),
                    mock.patch.object(asyncio, "sleep", new=mock.AsyncMock()),
                    self.assertRaisesRegex(
                        errors.TunnelError, "did not publish"
                    ),
                ):
                    await _connection.wait_for_offer(api, "simulation-1")
                api.get.assert_awaited_once()

    async def test_discovery_recovers_after_a_network_failure(self) -> None:
        api = _http.CovalApi(
            api_base_url="https://api.example.test/v1", api_key="test-key"
        )
        ready = {
            "status": "READY",
            "wss_url": "wss://relay.example.test",
            "token": "test-token",
            "expires_at": TOKEN_EXPIRES_AT_EPOCH_SECONDS,
        }
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            ready
        ).encode()
        with (
            mock.patch.object(
                _http.request,
                "urlopen",
                side_effect=[error.URLError("temporary DNS failure"), response],
            ),
            mock.patch.object(asyncio, "sleep", new=mock.AsyncMock()) as sleep,
        ):
            offer = await _connection.wait_for_offer(api, "simulation-1")
        self.assertEqual(offer.token, "test-token")
        sleep.assert_awaited_once_with(_defaults.POLL_INTERVAL_SECONDS)

    async def test_cleanup_preserves_session_error_without_exception_notes(
        self,
    ) -> None:
        class SessionWithoutNotesError(RuntimeError):
            add_note = None

        session_error = SessionWithoutNotesError("session failed")
        agent = mock.Mock()
        agent.close = mock.AsyncMock(
            side_effect=RuntimeError("agent cleanup failed")
        )
        exporter = mock.Mock()
        provider = mock.Mock()
        provider.force_flush.return_value = False
        with (
            mock.patch.object(
                _connection,
                "open_voice_connection",
                new=mock.AsyncMock(side_effect=session_error),
            ),
            mock.patch.object(
                _session.trace, "get_tracer_provider", return_value=provider
            ),
            self.assertRaisesRegex(
                SessionWithoutNotesError, "session failed"
            ) as raised,
        ):
            await _session.run_session(
                mock.Mock(), "simulation-1", lambda _id: agent, exporter
            )

        self.assertIs(raised.exception, session_error)
        agent.close.assert_awaited_once()
        provider.force_flush.assert_called_once()
        exporter.discard.assert_called_once_with(simulation_id="simulation-1")

    async def test_pcm_is_acknowledged_while_greeting_audio_is_still_sending(
        self,
    ) -> None:
        sending = asyncio.Event()
        sender_closed = asyncio.Event()
        acknowledgements: list[int] = []

        class Agent:
            async def on_ready(self) -> bytes:
                return b"\x00\x00"

            async def on_audio(self, audio: bytes) -> None:
                return None

            async def close(self) -> None:
                return None

        class Socket(SocketContext):
            async def send(self, message: str) -> None:
                acknowledgement = json.loads(message)
                self_type = acknowledgement["type"]
                assert self_type == "audio_ack"
                acknowledgements.append(acknowledgement["received_bytes"])

            async def __aiter__(self):
                yield SESSION_READY
                await sending.wait()
                yield b"\x01\x00"
                yield b"\x02\x00"
                assert acknowledgements == [2, 4]
                yield '{"type":"session_end"}'

        async def held_send(websocket, audio) -> None:
            sending.set()
            try:
                await asyncio.Event().wait()
            finally:
                sender_closed.set()

        with (
            mock.patch.object(
                _connection,
                "open_voice_connection",
                new=mock.AsyncMock(return_value=Socket()),
            ),
            mock.patch.object(_audio, "send_paced_audio", held_send),
        ):
            await asyncio.wait_for(
                _session.run_session(
                    mock.Mock(), "simulation-1", lambda _id: Agent()
                ),
                timeout=1,
            )

        self.assertEqual(acknowledgements, [2, 4])
        self.assertTrue(sender_closed.is_set())

    async def test_audio_sender_failure_wakes_a_waiting_receiver(self) -> None:
        class Agent:
            async def on_ready(self) -> bytes:
                return b"\x00\x00"

            async def close(self) -> None:
                return None

        class Socket(SocketContext):
            async def __aiter__(self):
                yield SESSION_READY
                await asyncio.Event().wait()

        with (
            mock.patch.object(
                _connection,
                "open_voice_connection",
                new=mock.AsyncMock(return_value=Socket()),
            ),
            mock.patch.object(
                _audio,
                "send_paced_audio",
                new=mock.AsyncMock(side_effect=OSError("Send failed.")),
            ),
            self.assertRaisesRegex(OSError, "Send failed"),
        ):
            await asyncio.wait_for(
                _session.run_session(
                    mock.Mock(), "simulation-1", lambda _id: Agent()
                ),
                timeout=1,
            )

    async def test_trace_failures_preserve_completed_sessions_and_agent_errors(
        self,
    ) -> None:
        class ClosingAgent:
            async def close(self) -> None:
                if cleanup_failure:
                    raise RuntimeError("agent cleanup failed")

        class EndingSocket(SocketContext):
            async def __aiter__(self):
                yield '{"type":"session_end"}'

        async def ready_offer(api, simulation_id):
            return _connection.ConnectionOffer(
                wss_url="wss://relay.example.test",
                token="test-token",
                expires_at=TOKEN_EXPIRES_AT_EPOCH_SECONDS,
            )

        api = _http.CovalApi(
            api_base_url="https://api.example.test/v1",
            api_key="test-key",
            workspace_id="workspace-a",
        )
        for cleanup_failure, trace_failure in itertools.product(
            (False, True), (None, "flush", "upload")
        ):
            with self.subTest(
                cleanup_failure=cleanup_failure, trace_failure=trace_failure
            ):
                exporter = mock.Mock()
                exporter.upload.return_value = 1
                if trace_failure == "upload":
                    exporter.upload.side_effect = tracing.CovalTraceError(
                        "trace upload failed"
                    )
                provider = mock.Mock()
                provider.force_flush.return_value = trace_failure != "flush"
                with (
                    mock.patch.object(
                        _connection, "wait_for_offer", ready_offer
                    ),
                    mock.patch.object(
                        _connection.client,
                        "connect",
                        new=mock.AsyncMock(return_value=EndingSocket()),
                    ),
                    mock.patch.object(
                        _session.trace,
                        "get_tracer_provider",
                        return_value=provider,
                    ),
                    (
                        self.assertRaisesRegex(
                            RuntimeError, "agent cleanup failed"
                        )
                        if cleanup_failure
                        else contextlib.nullcontext()
                    ) as raised,
                ):
                    await _session.run_session(
                        api,
                        "simulation-1",
                        lambda _id: ClosingAgent(),
                        exporter,
                    )
                if (
                    cleanup_failure
                    and trace_failure is not None
                    and hasattr(BaseException, "add_note")
                ):
                    self.assertIn(
                        "Trace cleanup also failed",
                        raised.exception.__notes__[0],
                    )
                if trace_failure != "flush":
                    exporter.upload.assert_called_once_with(
                        simulation_id="simulation-1",
                        api_base_url="https://api.example.test/v1",
                        api_key="test-key",
                        workspace_id="workspace-a",
                    )

    async def test_discovery_timeout_is_wrapped_on_python_310(self) -> None:
        class SeparateAsyncTimeoutError(Exception):
            pass

        async def timed_out_offer(api, simulation_id):
            raise SeparateAsyncTimeoutError("discovery timed out")

        with (
            mock.patch.object(
                asyncio, "TimeoutError", SeparateAsyncTimeoutError
            ),
            mock.patch.object(_connection, "wait_for_offer", timed_out_offer),
            self.assertRaisesRegex(
                errors.TunnelError, "connect deadline"
            ) as raised,
        ):
            await _connection.open_voice_connection(mock.Mock(), "simulation-1")
        self.assertIsInstance(
            raised.exception.__cause__, SeparateAsyncTimeoutError
        )

    async def test_transient_handshake_failure_reuses_the_same_offer(
        self,
    ) -> None:
        async def ready_offer(api, simulation_id):
            return _connection.ConnectionOffer(
                wss_url="wss://relay.example.test",
                token="test-token",
                expires_at=TOKEN_EXPIRES_AT_EPOCH_SECONDS,
            )

        connection = mock.AsyncMock()
        connector = mock.AsyncMock(
            side_effect=[OSError("transient network failure"), connection]
        )
        discover = mock.AsyncMock(side_effect=ready_offer)
        with (
            mock.patch.object(_connection, "wait_for_offer", discover),
            mock.patch.object(_connection.client, "connect", connector),
            mock.patch.object(asyncio, "sleep", new=mock.AsyncMock()),
        ):
            opened = await _connection.open_voice_connection(
                mock.Mock(), "simulation-1"
            )

        self.assertIs(opened, connection)
        self.assertEqual(connector.await_count, 2)
        self.assertEqual(
            connector.await_args.kwargs["ping_interval"],
            _defaults.WEBSOCKET_PING_INTERVAL_SECONDS,
        )
        self.assertEqual(
            connector.await_args.kwargs["ping_timeout"],
            _defaults.WEBSOCKET_PING_TIMEOUT_SECONDS,
        )
        self.assertEqual(discover.await_count, 1)
        self.assertEqual(
            [
                call.kwargs["additional_headers"]
                for call in connector.await_args_list
            ],
            [{"Authorization": "Bearer test-token"}] * 2,
        )

    async def test_socket_close_without_session_end_is_failure(self) -> None:
        class ClosingAgent:
            async def on_ready(self) -> None:
                return None

            async def close(self) -> None:
                return None

        class ClosingSocket(SocketContext):
            async def __aiter__(self):
                yield SESSION_READY

        exporter = mock.Mock()
        api = _http.CovalApi(
            api_base_url="https://api.example.test/v1", api_key="synthetic-key"
        )
        with (
            mock.patch.object(
                _connection,
                "open_voice_connection",
                new=mock.AsyncMock(return_value=ClosingSocket()),
            ),
            mock.patch.object(
                api,
                "get",
                new=mock.AsyncMock(
                    return_value={"simulation": {"status": "FAILED"}}
                ),
            ),
            self.assertRaisesRegex(errors.TunnelError, "before session_end"),
        ):
            await _session.run_session(
                api, "simulation-1", lambda _id: ClosingAgent(), exporter
            )

        exporter.upload.assert_not_called()
        exporter.discard.assert_called_once_with(simulation_id="simulation-1")

    async def test_relay_overload_reports_the_callback_backlog_and_closes_agent(
        self,
    ) -> None:
        agent = mock.Mock()
        agent.close = mock.AsyncMock()
        overload = exceptions.ConnectionClosedError(
            rcvd=frames.Close(code=1013, reason="Voice relay is overloaded."),
            sent=None,
        )

        class OverloadedSocket(SocketContext):
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise overload

        with (
            mock.patch.object(
                _connection,
                "open_voice_connection",
                new=mock.AsyncMock(return_value=OverloadedSocket()),
            ),
            self.assertRaisesRegex(
                errors.TunnelError, "audio backlog limit"
            ) as raised,
        ):
            await _session.run_session(
                mock.Mock(), "simulation-1", lambda _id: agent
            )
        self.assertIs(raised.exception.__cause__, overload)
        agent.close.assert_awaited_once()

    async def test_failed_session_cancels_other_agents(self) -> None:
        waiting_started = asyncio.Event()
        waiting_cancelled = asyncio.Event()

        async def fake_run_session(
            api, simulation_id, agent_factory, trace_exporter
        ) -> None:
            if simulation_id == "broken":
                await waiting_started.wait()
                raise errors.TunnelError("session failed")
            waiting_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                waiting_cancelled.set()

        config = run_config.RunConfig(
            agent_id="agent-1", persona_id="persona-1", test_set_id="test-set-1"
        )
        with (
            mock.patch.object(_session, "run_session", fake_run_session),
            mock.patch.object(_defaults, "POLL_INTERVAL_SECONDS", 0),
            self.assertRaisesRegex(errors.TunnelError, "session failed"),
        ):
            await _runs.launch_and_run(FakeApi(), config, lambda _id: None)

        self.assertTrue(waiting_cancelled.is_set())

    async def test_completed_conversation_missed_before_attachment_is_failure(
        self,
    ) -> None:
        class CompletedApi(FakeApi):
            async def get(
                self, path: str, *, query: dict | None = None
            ) -> dict:
                await asyncio.sleep(0)
                if path == "simulations":
                    return {
                        "simulations": [
                            {"simulation_id": "attached", "status": "RUNNING"},
                            {
                                "simulation_id": "missed",
                                "status": terminal_status,
                            },
                        ]
                    }
                return {"run": {"status": "COMPLETED"}}

        config = run_config.RunConfig(
            agent_id="agent-1", persona_id="persona-1", test_set_id="test-set-1"
        )
        for terminal_status in ("COMPLETED", "SKIPPED", "DELETED"):
            with self.subTest(terminal_status=terminal_status):
                run_session = mock.AsyncMock()
                with (
                    mock.patch.object(_session, "run_session", run_session),
                    self.assertRaisesRegex(
                        errors.TunnelError,
                        ("1 conversation.*finish before the SDK attached"),
                    ),
                ):
                    await _runs.launch_and_run(
                        CompletedApi(), config, lambda _id: None
                    )
                run_session.assert_awaited_once()
                self.assertEqual(run_session.await_args.args[1], "attached")


if __name__ == "__main__":
    unittest.main()
