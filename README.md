# Coval Agent SDK

Connect Python voice agents to Coval simulations through an authenticated outbound WebSocket. Your process runs the agent; Coval supplies the simulated caller, records the conversation, and evaluates the result. The agent does not need a public URL or a listening port.

Requires **Python 3.10 or newer**.

## Install

```sh
python -m pip install coval-agent-sdk
```

For OpenTelemetry support:

```sh
python -m pip install "coval-agent-sdk[tracing]"
```

The distribution is `coval-agent-sdk`; import it as `coval_agent_sdk`. See the [Agent SDK guide](https://docs.coval.ai/sdks/agent-sdk) for integration and configuration, and [PyPI](https://pypi.org/project/coval-agent-sdk/) for available versions. The SDK is classified as alpha; review release notes when upgrading.

## Connect an existing agent

Plug the SDK's audio connection into your framework's input and output transports. Your application owns agent construction, conversation state, provider clients, and pipeline cleanup.

Before running, select an existing **SDK WebSocket agent**, voice persona, and test set in your Coval workspace. Set `COVAL_API_KEY`, `COVAL_WORKSPACE_ID`, `COVAL_AGENT_ID`, `COVAL_PERSONA_ID`, and `COVAL_TEST_SET_ID` in your application environment. Use a test set with one case for a first run. If the SDK connection type is unavailable in your workspace, contact [Coval support](mailto:support@coval.dev).

The example below runs an **audio loopback**: it sends the caller's audio back unchanged. This checks the SDK connection; replace the handler with your agent's input/output integration to test agent behavior. Model-provider credentials stay in your application.

```python
import asyncio
import os

import coval_agent_sdk


async def on_connection(connection: coval_agent_sdk.AudioConnection) -> None:
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
        iterations=1,
        concurrency=1,
        run_timeout_seconds=600,
    )
    return await client.run(config, on_connection=on_connection)


if __name__ == "__main__":
    result = asyncio.run(main())
    print(result.run_id, result.status)
```

`on_connection` receives a ready `AudioConnection`. In an agent integration, feed `receive_audio()` into the pipeline and independently send its speech with `send_audio()`, so the agent can speak first. Keep the handler active until voice completion, then stop application tasks and finish spans before returning. A handler may run concurrently for different conversations; isolate their state in your application.

`agent_id` selects the hosted Coval agent configuration. The client uses the production API at `https://api.coval.dev/v1` by default; pass `api_base_url` to select another API root. `run_timeout_seconds` includes queueing and evaluation processing. A local timeout or cancellation stops local work but does not cancel the hosted run.

## Manage test configuration as code

Use the [Coval Terraform provider](https://docs.coval.ai/sdks/terraform-provider) to manage supported fixtures and pass their IDs to the SDK. Terraform owns the persistent configuration, your application runs the voice pipeline, and the Coval platform provides visual review of recordings, evaluations, and traces. The guide documents the current requirement for an existing SDK WebSocket agent.

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

A returned run has completed its execution; this does not mean the agent passed its quality criteria. A `ConversationResult` describes completed voice transport. With tracing disabled, the conversation's `trace` is `None`. Otherwise, `TraceResult.uploaded_spans` reports delivered spans after a successful upload (`None` when a failure leaves the count unknown), and `TraceResult.error` contains an optional flush/upload failure. The calling application decides how to store or display those outcomes.

Catch `CovalError` for SDK-owned failures, or a specific subclass such as `CovalHttpError`, `CovalConnectionError`, `RunError`, or `CovalTraceError`. Application exceptions and `asyncio.CancelledError` propagate unchanged after local cleanup; catching `CovalError` does not catch them.

For constructor and input checks, see [argument validation](docs/developer-api.md#argument-validation).

Failures raise instead of returning success. After a run is created, an SDK run failure raises `RunError` with a `result` snapshot containing its ID, last observed monitoring status (`None` before the first status response), and completed local conversations. Failed, cancelled, unfinished, or missed conversations do not appear as successful results. Local cancellation still propagates after cleanup and does not imply hosted cancellation.

## Optional traces

Install `coval-agent-sdk[tracing]` as shown above. The base installation does not install or import OpenTelemetry.

Import the optional exporter explicitly:

```python
from coval_agent_sdk import tracing

exporter = tracing.CovalTraceExporter()
```

Register it with your pipeline's OpenTelemetry setup, and pass the same exporter to `CovalVoiceClient(trace_exporter=...)`. Use a root span named `conversation` whose `conversation.id` attribute is the simulation ID available as `connection.simulation_id`. The SDK flushes ended spans and uploads each completed conversation separately.

Spans may contain prompts, transcripts, and model output. Enable upload only for data you intend to send to Coval. Failed-session traces are discarded after cleanup; a failed upload after a completed voice session is returned in `ConversationResult.trace.error` and can be retried through the retained exporter. See [trace lifecycle](docs/runtime.md#trace-lifecycle) for buffer limits and failure semantics.

## Develop and contribute

For editable installs and built-wheel testing, see [local development](docs/local-development.md).

[CONTRIBUTING.md](CONTRIBUTING.md) covers setup, tests, and review. [Python conventions](docs/python-style.md) define imports, initializers, Google-style docstrings, types, and formatting. [Architecture](docs/architecture.md) maps module ownership. [CHANGELOG.md](CHANGELOG.md) records user-facing changes, and [SECURITY.md](SECURITY.md) explains private vulnerability reporting.

## License

Copyright 2026 Coval, Inc. Licensed under the [Apache License, Version 2.0](LICENSE). See [NOTICE](NOTICE) for attribution.
