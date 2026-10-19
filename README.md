# coval-agents-sdk

Connect Python voice agents to Coval simulations through an authenticated outbound WebSocket. Your process runs the agent; Coval supplies the simulated caller, records the conversation, and evaluates the result. The agent does not need a public URL or a listening port.

The distribution is `coval-agent-sdk`, the Python package is `coval_agent_sdk`, and the CLI is `coval-agent`. Requires Python 3.10 or newer. This SDK is in pre-release development; install from a checkout while the API is being designed.

## Install from source

From this repository's root, install into an existing Python environment with `uv` or `pip`:

```sh
uv pip install .
# Or: python -m pip install .
```

For SDK development, use the editable setup instead:

```sh
scripts/install
.venv/bin/python -m coval_agent_sdk --help
```

See [local development](docs/local-development.md) for building a wheel and installing it into another project. A source install does not publish a package.

## Run an agent

Select an existing Coval agent, persona, and test set in your workspace. Set `COVAL_API_KEY`, `COVAL_WORKSPACE_ID`, `COVAL_AGENT_ID`, `COVAL_PERSONA_ID`, and `COVAL_TEST_SET_ID` in your environment. These are credentials and fixture identifiers for the Coval account you intend to test; model-provider credentials belong in your adapter.

```python
import asyncio
import os

import coval_agent_sdk
import my_agent


async def main() -> None:
    client = coval_agent_sdk.CovalVoiceClient(
        api_key=os.environ["COVAL_API_KEY"],
        workspace_id=os.environ["COVAL_WORKSPACE_ID"],
    )
    config = coval_agent_sdk.RunConfig(
        agent_id=os.environ["COVAL_AGENT_ID"],
        persona_id=os.environ["COVAL_PERSONA_ID"],
        test_set_id=os.environ["COVAL_TEST_SET_ID"],
    )
    await client.run(config, my_agent.create_agent)


if __name__ == "__main__":
    asyncio.run(main())
```

`my_agent` is your adapter module. Its `create_agent(simulation_id)` factory returns a fresh agent for each conversation. Use `await client.connect(simulation_id, my_agent.create_agent)` to attach to an existing simulation. Both client methods currently return `None` and print run/session identifiers; proposed result objects are described separately in [API design](docs/developer-api.md).

The client defaults to `https://api.coval.dev/v1`. Pass `api_base_url` to select another environment, and select the intended workspace explicitly when testing. An API key must be authorized for that workspace. Local installation, CLI help, and automated tests do not require an API key.

## Implement an adapter

Use `StreamingVoiceAgent` when ASR, language-model work, and TTS run in background tasks. Implement these methods:

| Method | Responsibility |
| --- | --- |
| `on_ready()` | Start the pipeline once the relay is ready, then return promptly. |
| `on_audio(audio)` | Enqueue incoming audio without waiting for remote processing. |
| `audio_output()` | Yield independently produced audio, including greetings and speech during input silence. |
| `close()` | Cancel and await the adapter's tasks and finish any conversation spans. |

Audio must be PCM16 little-endian, mono, 16 kHz. Bound your input and output queues. Yield even-length `bytes` chunks no larger than 2 MiB; normal 20 ms chunks contain 640 bytes. The SDK paces outgoing audio and closes the agent after success, failure, or cancellation. An output iterator that ends before the session ends is a failure.

The `VoiceAgent` callback protocol is also available: async `on_ready()` and `on_audio(bytes)` return optional reply bytes, and async `close()` releases resources. Use streaming output when speech must not wait for another input callback. Framework integrations belong in adapters, not in the transport package.

Read [runtime contracts](docs/runtime.md) before implementing an adapter. They cover receive-loop backpressure, pacing, timeouts, cancellation, and supported completion behavior. The SDK does not resample arbitrary audio or implement relay interruption/clear controls.

## Command line

The CLI adapter module exposes a zero-argument `create_agent()` factory. This differs from the Python client's simulation-ID factory and is a current design limitation.

```sh
coval-agent --workspace-id WORKSPACE_ID --agent-module my_agent \
  run --agent-id AGENT_ID --persona-id PERSONA_ID --test-set-id TEST_SET_ID

coval-agent --workspace-id WORKSPACE_ID --agent-module my_agent \
  connect SIMULATION_ID
```

`python -m coval_agent_sdk` accepts the same arguments. The CLI reads `COVAL_API_KEY`, `COVAL_WORKSPACE_ID`, and `COVAL_API_BASE_URL` from the environment; explicit workspace/base-URL flags take precedence. Use `run --run-timeout-seconds` to change the batch deadline, which defaults to one hour and includes queued conversations and evaluation postprocessing.

## Optional traces

Create a `CovalTraceExporter`, register it with your pipeline's OpenTelemetry setup, and pass the same exporter to `CovalVoiceClient(trace_exporter=...)`. Set the pipeline's conversation identifier to the simulation ID received by the agent factory. The SDK flushes ended spans and uploads each completed conversation separately.

Spans may contain prompts, transcripts, and model output. Enable upload only for data you intend to send to Coval. Failed-session traces are discarded after cleanup; a failed upload after a completed voice session is reported separately and can be retried through the retained exporter. See [trace lifecycle](docs/runtime.md#trace-lifecycle) for buffer limits and failure semantics.

## Develop and contribute

```sh
scripts/install
scripts/lint
scripts/check
```

[CONTRIBUTING.md](CONTRIBUTING.md) covers setup, tests, and review. [Python conventions](docs/python-style.md) define imports, initializers, Google-style docstrings, types, and formatting. [Architecture](docs/architecture.md) maps module ownership. [CHANGELOG.md](CHANGELOG.md) records user-facing changes, and [SECURITY.md](SECURITY.md) explains private vulnerability reporting.

## License

Copyright 2026 Coval, Inc. Licensed under the [Apache License, Version 2.0](LICENSE). See [NOTICE](NOTICE) for attribution.
