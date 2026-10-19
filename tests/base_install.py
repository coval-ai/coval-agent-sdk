"""Exercise the installed base wheel before any optional dependencies exist.

Executed by scripts/check in its clean wheel environment, outside the checkout.
The tracing-enabled suite runs afterward in a separate interpreter invocation.
"""

import asyncio
import contextlib
import importlib
from importlib import metadata
from importlib import util
import io
import json
import pathlib
import sys
import unittest
from unittest import mock

import coval_agent_sdk
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
PCM = b"\x01\x00" * 320


class Agent:
    def __init__(self):
        self.closed = False

    async def on_ready(self):
        return None

    async def on_audio(self, audio):
        return audio

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
        yield PCM
        await self.sent.wait()
        yield '{"type":"session_end"}'

    async def send(self, value):
        if isinstance(value, bytes):
            if value != PCM:
                raise AssertionError("Unexpected audio payload")
            self.sent.set()


class BaseInstallationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertIsNone(util.find_spec("opentelemetry"))
        self.assertTrue(
            pathlib.Path(coval_agent_sdk.__file__).is_relative_to(
                pathlib.Path(sys.prefix)
            )
        )
        installed = {item.metadata["Name"] for item in metadata.distributions()}
        self.assertFalse(
            any(name.startswith("opentelemetry-") for name in installed)
        )

    async def test_connect_and_run_without_optional_imports_or_output(self):
        client = coval_agent_sdk.CovalVoiceClient(api_key="synthetic-key")
        agents = []
        sockets = []

        def factory(simulation_id):
            agent = Agent()
            agents.append(agent)
            return agent

        async def connect(api, simulation_id):
            socket = Socket()
            sockets.append(socket)
            return socket

        async def read(path, *, query=None):
            await asyncio.sleep(0)
            if path == "simulations":
                return {
                    "simulations": [
                        {"simulation_id": "conversation-a", "status": "RUNNING"}
                    ]
                }
            return {"run": {"status": "COMPLETED"}}

        output, errors = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(_connection, "open_voice_connection", connect),
            mock.patch.object(
                client._api,
                "post",
                mock.AsyncMock(return_value={"run": {"run_id": "run-a"}}),
            ),
            mock.patch.object(client._api, "get", read),
            mock.patch.object(_defaults, "POLL_INTERVAL_SECONDS", 0),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
            self.assertNoLogs("coval_agent_sdk", level="DEBUG"),
        ):
            session = await asyncio.wait_for(
                client.connect("direct-a", factory), 2
            )
            run = await asyncio.wait_for(
                client.run(
                    coval_agent_sdk.RunConfig(
                        "agent-a", "persona-a", "test-set-a"
                    ),
                    factory,
                ),
                2,
            )
        self.assertEqual(
            session, coval_agent_sdk.SessionResult("direct-a", 640, 640)
        )
        self.assertEqual(
            run,
            coval_agent_sdk.RunResult(
                "run-a",
                "COMPLETED",
                (coval_agent_sdk.SessionResult("conversation-a", 640, 640),),
            ),
        )
        self.assertTrue(all(agent.closed for agent in agents))
        self.assertTrue(all(socket.closed for socket in sockets))
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")
        self.assertFalse(
            any(name.startswith("opentelemetry") for name in sys.modules)
        )

    def test_concrete_tracing_requires_the_optional_dependency(self):
        self.assertFalse(hasattr(coval_agent_sdk, "CovalTraceExporter"))
        with self.assertRaises(ModuleNotFoundError) as failure:
            importlib.import_module("coval_agent_sdk.tracing")
        self.assertEqual(failure.exception.name, "opentelemetry")


if __name__ == "__main__":
    unittest.main()
