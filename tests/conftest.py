"""Loopback Coval API and TLS fixtures shared by public wire journeys."""

from http import server as http_server
import json
import shutil
import ssl
import subprocess
import threading
import time
from urllib import parse

import pytest

IDS = ("conversation-a", "conversation-b")


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
            elif target.path in {
                f"/v1/simulations/{name}" for name in state["ids"]
            }:
                name = target.path.rsplit("/", 1)[1]
                response = {
                    "simulation": {
                        "status": "COMPLETED"
                        if name in state["completed"]
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
