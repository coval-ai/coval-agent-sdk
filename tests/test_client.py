"""Focused lifecycle tests for the importable voice client."""

import asyncio
import json
import time
import unittest
from unittest import mock

import urllib3
from urllib3 import exceptions as http_exceptions
from urllib3 import response as http_response
from websockets import datastructures
from websockets import exceptions
from websockets import http11

from coval_agent_sdk import _connection
from coval_agent_sdk import _defaults
from coval_agent_sdk import _http
from coval_agent_sdk import _runs
from coval_agent_sdk import _session
from coval_agent_sdk import config as run_config
from coval_agent_sdk import errors
from coval_agent_sdk import results

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

                async def session(*args) -> results.ConversationResult:
                    nonlocal cancelled
                    if expires:  # noqa: B023 - The session is awaited within this case.
                        try:
                            await asyncio.Event().wait()
                        except asyncio.CancelledError:
                            cancelled = True
                            raise
                    return results.ConversationResult("attached", 0, 0)

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
                            errors.RunError, "within 10 seconds"
                        ) as failure:
                            await _runs.launch_and_run(
                                BatchApi(), config, mock.Mock()
                            )
                        self.assertEqual(
                            failure.exception.result.run_id, "run-1"
                        )
                        self.assertEqual(
                            failure.exception.result.status, "RUNNING"
                        )
                        self.assertEqual(
                            failure.exception.result.conversations, ()
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

        async def session(*args) -> results.ConversationResult:
            nonlocal session_cancelled
            try:
                await session_finished.wait()
            except asyncio.CancelledError:
                session_cancelled = True
                raise
            return results.ConversationResult("attached", 0, 0)

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
        failures.append(errors.CovalConnectionError("unreachable"))
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
                    self.assertRaisesRegex(errors.CovalError, "retry deadline"),
                ):
                    await _runs._get_monitoring_resource(
                        api, "runs/run-1", run_deadline=remaining_run_seconds
                    )

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
            self.assertRaisesRegex(errors.CovalError, "retry deadline"),
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
                    self.assertRaisesRegex(errors.CovalError, status),
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
                        errors.CovalError, "did not publish"
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
        response = http_response.HTTPResponse(
            json.dumps(ready).encode(), status=200
        )
        with (
            mock.patch.object(
                urllib3.PoolManager,
                "request",
                side_effect=[
                    http_exceptions.NewConnectionError(None, "DNS failure"),
                    response,
                ],
            ),
            mock.patch.object(asyncio, "sleep", new=mock.AsyncMock()) as sleep,
        ):
            offer = await _connection.wait_for_offer(api, "simulation-1")
        self.assertEqual(offer.token, "test-token")
        sleep.assert_awaited_once_with(_defaults.POLL_INTERVAL_SECONDS)

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
                errors.CovalError, "connect deadline"
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

        interrupted = exceptions.InvalidMessage("connection ended")
        interrupted.__cause__ = EOFError()
        failures = [OSError("network"), asyncio.TimeoutError(), interrupted]
        failures.extend(
            exceptions.InvalidStatus(
                http11.Response(status, "temporary", datastructures.Headers())
            )
            for status in (500, 502, 503, 504)
        )
        for failure in failures:
            with self.subTest(failure=failure):
                connection = mock.AsyncMock()
                connector = mock.AsyncMock(side_effect=[failure, connection])
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

    async def test_permanent_handshake_failure_is_not_retried(self) -> None:
        failures = [
            exceptions.InvalidStatus(
                http11.Response(status, "rejected", datastructures.Headers())
            )
            for status in (400, 401, 403, 429)
        ]
        failures.append(exceptions.InvalidHandshake("invalid response"))
        for failure in failures:
            with self.subTest(failure=failure):
                discover = mock.AsyncMock(
                    return_value=_connection.ConnectionOffer(
                        wss_url="wss://relay.example.test",
                        token="synthetic-token",
                        expires_at=TOKEN_EXPIRES_AT_EPOCH_SECONDS,
                    )
                )
                connector = mock.AsyncMock(side_effect=[failure, mock.Mock()])
                with (
                    mock.patch.object(_connection, "wait_for_offer", discover),
                    mock.patch.object(_connection.client, "connect", connector),
                    mock.patch.object(
                        asyncio, "sleep", new=mock.AsyncMock()
                    ) as sleep,
                    self.assertRaises(errors.CovalError) as raised,
                ):
                    await _connection.open_voice_connection(
                        mock.Mock(), "simulation-1"
                    )
                self.assertIs(raised.exception.__cause__, failure)
                connector.assert_awaited_once()
                discover.assert_awaited_once()
                sleep.assert_not_awaited()

    async def test_failed_session_cancels_other_connections(self) -> None:
        waiting_started = asyncio.Event()
        waiting_cancelled = asyncio.Event()

        async def fake_run_session(
            api, simulation_id, on_connection, trace_exporter
        ) -> None:
            if simulation_id == "broken":
                await waiting_started.wait()
                raise errors.CovalError("session failed")
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
            self.assertRaisesRegex(errors.CovalError, "session failed"),
        ):
            await _runs.launch_and_run(FakeApi(), config, lambda _id: None)

        self.assertTrue(waiting_cancelled.is_set())

    async def test_unattached_conversation_is_failure(
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
        for terminal_status in (
            "COMPLETED",
            "SKIPPED",
            "DELETED",
            "PENDING",
            "IN QUEUE",
        ):
            with self.subTest(terminal_status=terminal_status):
                run_session = mock.AsyncMock()
                with (
                    mock.patch.object(_session, "run_session", run_session),
                    self.assertRaisesRegex(
                        errors.CovalError,
                        ("1 conversation.*not attached before the run ended"),
                    ),
                ):
                    await _runs.launch_and_run(
                        CompletedApi(), config, lambda _id: None
                    )
                run_session.assert_awaited_once()
                self.assertEqual(run_session.await_args.args[1], "attached")


if __name__ == "__main__":
    unittest.main()
