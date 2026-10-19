"""Exercise the real API SDK behind the private JSON boundary."""

import asyncio
from http import server
import json
import logging
import threading
from unittest import mock
from urllib import parse

from coval_sdk import exceptions as api_exceptions
from opentelemetry import trace
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.sdk import trace as sdk_trace
import pytest
import urllib3
from urllib3 import exceptions as http_exceptions

import coval_agent_sdk
from coval_agent_sdk import _http
from coval_agent_sdk import tracing


@pytest.fixture
def endpoint():
    requests = []
    reply = {"status": 200, "body": b'{"ok":true}', "drop": False}

    class Handler(server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            requests.append(
                (
                    self.command,
                    self.path,
                    dict(self.headers),
                    self.rfile.read(int(self.headers.get("Content-Length", 0))),
                )
            )
            if reply["drop"]:
                self.close_connection = True
                return
            self.send_response(reply["status"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(reply["body"])

        def log_message(self, *args):
            pass

    http = server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}/v1", requests, reply
    finally:
        http.shutdown()
        thread.join(timeout=2)
        http.server_close()


@pytest.mark.parametrize("status", [200, 400, 503])
def test_real_trace_request_encodes_otlp_and_stays_silent(
    endpoint, caplog, capsys, status
):
    url, requests, reply = endpoint
    reply["status"] = status
    collector = tracing.CovalTraceExporter()
    span = sdk_trace.ReadableSpan(
        name="conversation",
        context=trace.SpanContext(trace_id=1, span_id=2, is_remote=False),
        attributes={"conversation.id": "simulation-a"},
        start_time=1,
        end_time=2,
    )
    collector.export([span])
    options = {
        "simulation_id": "simulation-a",
        "api_base_url": url,
        "api_key": "synthetic-key",
        "workspace_id": "workspace-a",
    }
    if status == 200:
        assert collector.upload(**options) == 1
    else:
        with pytest.raises(coval_agent_sdk.CovalTraceError, match=str(status)):
            collector.upload(**options)
        assert collector._buffered_span_count == 1
    assert len(requests) == 1
    method, path, headers, body = requests[0]
    assert (method, path) == ("POST", "/v1/traces")
    headers = {key.lower(): value for key, value in headers.items()}
    assert headers["content-type"] == "application/x-protobuf"
    assert headers["x-api-key"] == "synthetic-key"
    assert headers["x-coval-workspace-id"] == "workspace-a"
    assert headers["x-simulation-id"] == "simulation-a"
    decoded = trace_service_pb2.ExportTraceServiceRequest.FromString(body)
    encoded = decoded.resource_spans[0].scope_spans[0].spans
    assert len(encoded) == 1
    assert encoded[0].name == "conversation"
    assert encoded[0].trace_id == (1).to_bytes(16, "big")
    assert encoded[0].span_id == (2).to_bytes(8, "big")
    assert capsys.readouterr() == ("", "")
    assert caplog.records == []
    if status != 200:
        reply["status"] = 200
        assert collector.upload(**options) == 1
        assert len(requests) == 2
        assert requests[1][3] == body
    assert collector._buffered_span_count == 0


@pytest.mark.asyncio
async def test_real_sdk_serializes_queries_headers_and_payloads(endpoint):
    url, requests, _ = endpoint
    api = _http.CovalApi(
        api_base_url=url + "/",
        api_key="synthetic-key",
        workspace_id="workspace-a",
    )
    assert await api.get(
        "simulations",
        query={"filter": 'run_id="run-a"', "page_token": "a+b /?"},
    ) == {"ok": True}
    assert await api.post("runs", {"dev_id": "dev-a", "num_iterations": 2}) == {
        "ok": True
    }
    assert [item[0] for item in requests] == ["GET", "POST"]
    path = parse.urlsplit(requests[0][1])
    assert path.path == "/v1/simulations"
    assert parse.parse_qs(path.query) == {
        "filter": ['run_id="run-a"'],
        "page_token": ["a+b /?"],
    }
    assert requests[1][1] == "/v1/runs"
    assert json.loads(requests[1][3]) == {
        "dev_id": "dev-a",
        "num_iterations": 2,
    }
    for _, _, headers, _ in requests:
        headers = {key.lower(): value for key, value in headers.items()}
        assert headers["x-api-key"] == "synthetic-key"
        assert headers["x-coval-workspace-id"] == "workspace-a"


@pytest.mark.asyncio
@pytest.mark.parametrize("drop", [False, True])
async def test_public_run_launch_is_not_retried(endpoint, drop):
    url, requests, reply = endpoint
    reply.update(status=503, body=b"temporary outage", drop=drop)
    voice = coval_agent_sdk.CovalVoiceClient(
        api_key="synthetic-key", api_base_url=url
    )
    expected = (
        coval_agent_sdk.CovalConnectionError
        if drop
        else coval_agent_sdk.CovalHttpError
    )
    with pytest.raises(expected) as failure:
        await voice.run(
            coval_agent_sdk.RunConfig(
                agent_id="agent-a", persona_id="persona-a", test_set_id="set-a"
            ),
            lambda _: pytest.fail("Launch failed before agent creation"),
        )
    assert type(failure.value).__module__ == "coval_agent_sdk.errors"
    assert not hasattr(voice, "api")
    assert len(requests) == 1
    assert requests[0][0:2] == ("POST", "/v1/runs")
    assert json.loads(requests[0][3])["agent_id"] == "agent-a"
    if not drop:
        assert failure.value.status_code == 503
        assert "temporary outage" in str(failure.value)


@pytest.mark.asyncio
async def test_transport_leaves_read_retries_to_orchestration(endpoint):
    url, requests, reply = endpoint
    reply["drop"] = True
    api = _http.CovalApi(api_base_url=url, api_key="synthetic-key")
    with pytest.raises(coval_agent_sdk.CovalConnectionError):
        await api.get("simulations")
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "message"),
    [(b"not-json", "invalid JSON"), (b"[]", "non-object JSON")],
)
async def test_invalid_discovery_response_raises_agent_sdk_error(
    endpoint, payload, message
):
    url, requests, reply = endpoint
    reply["body"] = payload
    voice = coval_agent_sdk.CovalVoiceClient(
        api_key="synthetic-key", api_base_url=url
    )
    with pytest.raises(coval_agent_sdk.TunnelError, match=message):
        await voice.connect("simulation-a", lambda _: None)
    assert len(requests) == 1
    assert requests[0][1] == "/v1/simulations/simulation-a/connect"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (None, None),
        (
            api_exceptions.ApiException(status=0),
            coval_agent_sdk.CovalConnectionError,
        ),
        (
            api_exceptions.ApiException(status=403),
            coval_agent_sdk.CovalHttpError,
        ),
        (
            http_exceptions.ReadTimeoutError(None, None, "timeout"),
            coval_agent_sdk.CovalConnectionError,
        ),
        (OSError("connection reset"), coval_agent_sdk.CovalConnectionError),
    ],
)
async def test_request_timeout_cleanup_and_error_translation(outcome, expected):
    api = _http.CovalApi(
        api_base_url="https://api.example.test/v1", api_key="synthetic-key"
    )
    response = urllib3.HTTPResponse(b'{"ok":true}', status=200)
    with (
        mock.patch.object(
            urllib3.PoolManager,
            "request",
            return_value=response,
            side_effect=outcome,
        ) as request,
        mock.patch.object(urllib3.PoolManager, "clear") as clear,
    ):
        if expected is None:
            assert await api.get("simulations/simulation-a") == {"ok": True}
            assert response.closed
        else:
            with pytest.raises(expected) as failure:
                await api.get("simulations/simulation-a")
            assert failure.value.__cause__ is None
            if isinstance(failure.value, coval_agent_sdk.CovalHttpError):
                assert failure.value.status_code == 403
        clear.assert_called_once()
        request.assert_called_once()
        assert request.call_args.kwargs["timeout"].total == 15
        headers = request.call_args.kwargs["headers"]
        assert "X-Coval-Workspace-Id" not in headers


@pytest.mark.asyncio
async def test_cancelled_request_releases_resources_when_worker_finishes():
    started = threading.Event()
    release = threading.Event()
    cleared = threading.Event()

    def request(*args, **kwargs):
        started.set()
        if not release.wait(timeout=2):
            raise TimeoutError("Test did not release request")
        return urllib3.HTTPResponse(b'{"ok":true}', status=200)

    with (
        mock.patch.object(urllib3.PoolManager, "request", side_effect=request),
        mock.patch.object(
            urllib3.PoolManager, "clear", side_effect=cleared.set
        ),
    ):
        api = _http.CovalApi(
            api_base_url="https://api.example.test/v1", api_key="synthetic-key"
        )
        task = asyncio.create_task(api.get("simulations/simulation-a"))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            assert await asyncio.to_thread(cleared.wait, 2)


@pytest.mark.asyncio
async def test_requests_preserve_application_logging(endpoint, caplog, capsys):
    url, _, _ = endpoint
    names = ("", "coval_sdk", "urllib3")
    before = {
        name: (
            logging.getLogger(name).level,
            logging.getLogger(name).handlers[:],
        )
        for name in names
    }
    api = _http.CovalApi(api_base_url=url, api_key="synthetic-key")
    assert await api.get("simulations") == {"ok": True}
    assert caplog.records == []
    assert capsys.readouterr() == ("", "")
    assert before == {
        name: (
            logging.getLogger(name).level,
            logging.getLogger(name).handlers[:],
        )
        for name in names
    }
