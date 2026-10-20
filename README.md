# coval-agents-sdk

Connect Python voice agents to Coval simulations through an authenticated outbound WebSocket. Your process runs the agent; Coval supplies the simulated caller, records the conversation, and evaluates the result. The agent does not need a public URL or a listening port.

The distribution is `coval-agent-sdk`, the Python package is `coval_agent_sdk`, and the API is a Python library. Requires Python 3.10 or newer. The initial release candidate is `0.1.0`; install from a checkout until it is published.

## Install from source

From this repository's root, install into an existing Python environment with `uv` or `pip`:

```sh
uv pip install .
# Or: python -m pip install .
```

For SDK development, use the editable setup instead:

```sh
scripts/install
.venv/bin/python -c "import coval_agent_sdk"
```

See [local development](docs/local-development.md) for building a wheel and installing it into another project. A source install does not publish a package. Maintainers can follow [releasing](docs/releasing.md) to configure PyPI publishing.

## Connect an existing agent

The SDK exposes an audio connection, not an agent class. Plug it into your framework's input and output transports. Your application owns agent construction, conversation state, provider clients, and pipeline cleanup.

Select existing Coval agent, persona, and test-set fixtures in the intended workspace. Configure credentials through your application environment; model-provider credentials stay in your application.

```python
import asyncio
import os

import coval_agent_sdk


async def on_connection(connection: coval_agent_sdk.AudioConnection) -> None:
    # Minimal audio passthrough to demonstrate the connection contract.
    # A real integration feeds caller audio into its existing pipeline and
    # independently sends the pipeline's speech with send_audio().
    async for audio in connection.receive_audio():
        await connection.send_audio(audio)


async def main() -> coval_agent_sdk.RunResult:
    client = coval_agent_sdk.CovalVoiceClient(
        api_key=os.environ["COVAL_API_KEY"],
        workspace_id=os.environ["COVAL_WORKSPACE_ID"],
    )
    config = coval_agent_sdk.RunConfig(
        agent_id=os.environ["COVAL_AGENT_ID"],
        persona_id=os.environ["COVAL_PERSONA_ID"],
        test_set_id=os.environ["COVAL_TEST_SET_ID"],
    )
    return await client.run(config, on_connection=on_connection)


if __name__ == "__main__":
    asyncio.run(main())
```

`on_connection` is an async connection handler, not a factory: it receives a ready `AudioConnection` and returns nothing. It wires your application into the audio exchange, remains active until the conversation ends, then stops its tasks and finishes its spans before returning. Different conversations may invoke it concurrently; the application or framework owns isolation of conversation state. The SDK never constructs, subclasses, or closes your agent.

`agent_id` selects an existing hosted Coval fixture; it is not a Python object or class. The client defaults to `https://api.coval.dev/v1`; pass `api_base_url` to select another environment. Select the intended workspace explicitly when testing. The Coval API SDK stays private: applications use only this package's client, configuration, audio connection, results, and errors.

## Attach to an existing simulation

`connect()` opens one existing simulation without creating a run. It is an async context manager that yields only after the relay is ready:

```python
async def attach(
    client: coval_agent_sdk.CovalVoiceClient, simulation_id: str
) -> coval_agent_sdk.ConversationResult | None:
    async with client.connect(simulation_id) as connection:
        await on_connection(connection)
    return connection.result
```

The context closes transport and finalizes traces after application code exits. `connection.result` is available after successful exit; leaving before voice completion raises instead of reporting success. `run()` returns a `RunResult` containing these per-conversation outcomes. Neither method provides a CLI, logs, or console output.

## Audio connection

| Member | Contract |
| --- | --- |
| `simulation_id` | Correlate application state and traces with this conversation. |
| `receive_audio()` | Async iterator of caller PCM; ends normally only on successful voice completion. Use one receive loop. |
| `send_audio(audio)` | Await a paced write. Returns `True` for a complete write, `False` when normal completion interrupts it; failures raise. |
| `wait_closed()` | Wait for voice completion without consuming input; failures raise. |
| `closed` | Whether the exchange has ended, including failure. |
| `result` | Finalized `ConversationResult` after successful context exit, otherwise `None`. |

Input and output are independent, so the pipeline may greet the caller before receiving any audio. Audio is PCM16 little-endian, mono, 16 kHz. A normal 20 ms frame contains 640 bytes. The SDK does not resample or implement relay interruption/clear controls. Framework integrations own conversion and interruption policy.

Drain incoming audio promptly into a bounded pipeline; unread input is limited to 64 KiB. Await output writes and bound framework queues rather than spawning unlimited send tasks. Chunks must be even-length `bytes`, at most 2 MiB. Output is serialized and paced across adjacent writes. Read [runtime contracts](docs/runtime.md) for readiness, backpressure, deadlines, cancellation, and cleanup.

## Results and failures

`RunResult` contains `run_id`, the hosted `status`, and a tuple of completed `conversations`. Each `ConversationResult` contains `simulation_id`, local voice `status`, `received_audio_bytes`, `sent_audio_bytes`, and an optional `trace` outcome. These immutable objects contain no relay credentials or transport objects.

A returned run has completed its execution; this does not mean the agent passed its quality criteria. A returned session describes completed voice transport, not hosted evaluation. With tracing disabled, `session.trace` is `None`. Otherwise, `TraceResult.uploaded_spans` reports delivered spans after a successful upload (`None` when a failure leaves the count unknown), and `TraceResult.error` contains an optional flush/upload failure. The calling application decides how to store or display those outcomes.

Catch `CovalError` for SDK-owned failures, or a specific subclass such as `CovalHttpError`, `CovalConnectionError`, `RunError`, or `CovalTraceError`. Adapter exceptions and `asyncio.CancelledError` propagate unchanged after local cleanup; catching `CovalError` does not catch them.

For constructor and input checks, see [argument validation](docs/developer-api.md#argument-validation).

Failures raise instead of returning success. After a run is created, an SDK run failure raises `RunError` with a `result` snapshot containing its ID, last observed monitoring status (`None` before the first status response), and completed local conversations. Failed, cancelled, unfinished, or missed conversations do not appear as successful results. Local cancellation still propagates after cleanup and does not imply hosted cancellation.

## Optional traces

Install the `tracing` extra from the checkout with `python -m pip install ".[tracing]"`, or request `[tracing]` when installing a built wheel. The base installation does not install or import OpenTelemetry.

Import the optional exporter explicitly:

```python
from coval_agent_sdk import tracing

exporter = tracing.CovalTraceExporter()
```

Register it with your pipeline's OpenTelemetry setup, and pass the same exporter to `CovalVoiceClient(trace_exporter=...)`. Use a root span named `conversation` whose `conversation.id` attribute is the simulation ID available as `connection.simulation_id`. The SDK flushes ended spans and uploads each completed conversation separately.

Spans may contain prompts, transcripts, and model output. Enable upload only for data you intend to send to Coval. Failed-session traces are discarded after cleanup; a failed upload after a completed voice session is returned in `ConversationResult.trace.error` and can be retried through the retained exporter. See [trace lifecycle](docs/runtime.md#trace-lifecycle) for buffer limits and failure semantics.

## Develop and contribute

```sh
scripts/install
scripts/lint
scripts/check
```

[CONTRIBUTING.md](CONTRIBUTING.md) covers setup, tests, and review. [Python conventions](docs/python-style.md) define imports, initializers, Google-style docstrings, types, and formatting. [Architecture](docs/architecture.md) maps module ownership. [CHANGELOG.md](CHANGELOG.md) records user-facing changes, and [SECURITY.md](SECURITY.md) explains private vulnerability reporting.

## License

Copyright 2026 Coval, Inc. Licensed under the [Apache License, Version 2.0](LICENSE). See [NOTICE](NOTICE) for attribution.
