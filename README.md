# Coval Agent SDK

Connect your Python voice pipeline to Coval's simulated callers. Your application runs the agent; Coval records and evaluates the conversation. The SDK uses an outbound WebSocket, so your agent does not need a public endpoint.

Requires **Python 3.10 or newer**.

## Install

```sh
python -m pip install coval-agent-sdk
```

Import the package as `coval_agent_sdk`. See the [Agent SDK guide](https://docs.coval.ai/sdks/agent-sdk) for integration details and [PyPI](https://pypi.org/project/coval-agent-sdk/) for available versions.

## Run a conversation

A run uses an SDK WebSocket agent, voice persona, and test set from the same workspace. Pass credentials and resource IDs to the client and `RunConfig`; this example reads them from environment variables.

The handler below echoes the caller's audio to check the connection. In an agent integration, the handler connects the audio stream to your voice pipeline.

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

`on_connection` receives a ready `AudioConnection`. Feed `receive_audio()` into your pipeline and independently send its speech with `send_audio()`. Keep the handler active until the conversation ends, then finish conversation tasks and spans before returning. Handlers may run concurrently; keep conversation state separate.

The client defaults to `https://api.coval.dev/v1`. Set `api_base_url` to use another API root. Audio is PCM16 little-endian, mono, 16 kHz; see the [audio guide](https://docs.coval.ai/sdks/agent-sdk#audio-format-and-limits) for format and buffering requirements.

## Connect to an existing simulation

`connect()` attaches to one simulation without creating a run:

```python
async def attach(
    client: coval_agent_sdk.CovalVoiceClient, simulation_id: str
) -> coval_agent_sdk.ConversationResult | None:
    async with client.connect(simulation_id) as connection:
        await on_connection(connection)
    return connection.result
```

Read `connection.result` after the context exits successfully. Leaving before voice completion raises an error. Local timeout or cancellation stops local work without cancelling the hosted run.

## Results and errors

`RunResult` contains the run ID, hosted status, completed `conversations`, and SDK session `failures`. Each `ConversationFailure` retains the simulation ID, exception type and diagnostic message. Each `ConversationResult` includes its simulation ID, audio byte counts, and optional trace delivery result. Execution completion does not mean the agent passed its evaluation criteria.

Catch `CovalError` for SDK failures. A failed voice session is isolated while the SDK continues attaching and serving unrelated conversations. After the batch finishes, `RunError.result` retains the run ID, last observed status, completed conversations, and per-session failures. Authentication/authorization errors, global monitoring errors and the batch deadline still stop local work promptly. Application exceptions and cancellation propagate unchanged. See [Developer API](docs/developer-api.md) for result types and argument validation.

## Tracing

```sh
python -m pip install "coval-agent-sdk[tracing]"
```

Share one `CovalTraceExporter` between your OpenTelemetry provider and `CovalVoiceClient(trace_exporter=...)`. Correlate the conversation root with `connection.simulation_id` and finish spans before the handler returns. The [tracing guide](https://docs.coval.ai/sdks/agent-sdk#optional-opentelemetry-traces) includes a setup example.

## Terraform

[Manage test configuration with Terraform](https://docs.coval.ai/sdks/terraform-provider) and pass the resulting resource IDs to the SDK. Use Coval to review recordings, evaluations, and traces.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) covers setup, tests, and review. See [local development](docs/local-development.md), [Python conventions](docs/python-style.md), and [architecture](docs/architecture.md) for contributor guidance. [CHANGELOG.md](CHANGELOG.md) records releases; [SECURITY.md](SECURITY.md) explains private vulnerability reporting.

## License

Copyright 2026 Coval, Inc. Licensed under the [Apache License, Version 2.0](LICENSE). See [NOTICE](NOTICE) for attribution.
