# coval-agent-sdk

A standalone Python Voice Agent SDK developed in the private `coval-ai/coval-agent-sdk` repository. The installable distribution is `coval-agent-sdk==0.2.0a1`; Python imports remain `coval_voice` while the developer API is being designed. No package-registry release is published by this project.

Connect a Python voice agent outbound to Coval simulations using an organization/workspace API key. No listening port, public agent URL, AWS credentials, or backend imports are required on the agent machine. The distribution is `coval-agent-sdk`; the public Python namespace is `coval_voice`. Requires Python 3.10+ and WebSockets 15.x.

## Develop locally

The implementation is introduced in a draft PR against the README-only initial commit. The commands below apply to the implementation branch until that PR merges. From this project directory, with Python 3.12 and `uv` available:

```bash
scripts/install
.venv/bin/coval-tunnel --help
scripts/lint
scripts/check
```

`scripts/install` creates this project's virtual environment and installs the SDK as an editable package. Changes to its Python source are immediately visible in that environment. `scripts/check` builds a source distribution and wheel, checks metadata, and exercises the installed wheel in a separate temporary environment outside the project. These commands make no provider calls or simulations.

To install into another existing development environment without publishing:

```bash
uv pip install --python /absolute/path/to/consumer/.venv/bin/python -e /absolute/path/to/coval-agent-sdk
```

For an isolated consumer check, build with `scripts/build` and install the wheel from `dist/`. The existing Agent Playground PR can consume that wheel through `COVAL_VOICE_SDK_WHEEL`. Its default install still expects a package-index release; committed dependency rewiring is deferred until the developer API settles. A private Git dependency will require GitHub access for both developers and consumer CI.

Read [contributing](CONTRIBUTING.md) and [Python conventions](docs/python-style.md) for repository layout, imports, initializers, Google docstrings, Ruff, and strict type checking.

Read [local development](docs/local-development.md), [developer API design](docs/developer-api.md), and [extraction provenance](docs/extraction.md). The design document labels proposed interfaces separately from the executable API described below. This package is separate from the generated `coval-sdk`.

## Use as a library

After installation, import the client and pass a factory that creates a fresh agent for every conversation:

```python
import asyncio
import os

import coval_voice
import my_agent

client = coval_voice.CovalVoiceClient(
    api_key=os.environ["COVAL_API_KEY"],
    workspace_id=os.environ["COVAL_WORKSPACE_ID"],
)
config = coval_voice.RunConfig(
    agent_id="AGENT_ID",
    persona_id="PERSONA_ID",
    test_set_id="TEST_SET_ID",
)
asyncio.run(client.run(config, my_agent.create_agent))
```

`create_agent(simulation_id)` returns an object with async `on_ready()`, `on_audio(bytes)`, and `close()` methods. Every call creates a fresh agent instance. The client fetches the WSS offer, opens the outbound connection, paces PCM16 audio, and closes the agent when the session ends. `client.connect(simulation_id, create_agent)` attaches to an already-created simulation instead.

The total batch deadline defaults to one hour. Set `RunConfig(run_timeout_seconds=7200, ...)` or CLI `run --run-timeout-seconds 7200 ...` for larger test sets, more iterations, or low concurrency. This deadline includes queued conversations and postprocessing. Expiry raises an error and cancels any active local sessions, so choose a budget for the whole batch. Individual discovery and monitoring outage retries remain bounded.

`on_audio` runs on the socket receive loop and must return promptly. Enqueue audio into a bounded agent queue and perform slow ASR or remote processing in a separate task. Sustained processing must keep pace with incoming audio, normally one 20 ms chunk at a time. Acknowledgements are sent when each chunk is received, but the next chunk cannot be received while the callback awaits. The relay permits 64 KiB of unacknowledged audio, approximately 2.05 seconds at 16 kHz mono PCM16. Exceeding that backlog closes the connection with code 1013 and an actionable SDK error. This budget covers cumulative backlog, not a fresh two-second allowance for every callback.

The worker joins the relay before publishing a connection offer. An SDK upgrade is rejected with a retryable conflict if that worker has disappeared. After an upgrade, the SDK waits at most 300 seconds for `session_ready`, then closes the connection and agent with an error. This also bounds a worker failure between the upgrade and readiness. A successfully consumed join is not replayed.

If the relay closes after readiness without delivering `session_end`, the client gives the simulation API up to ten seconds to confirm `COMPLETED`. Only that explicit persisted outcome permits normal cleanup and trace upload. Failed, cancelled, malformed, unavailable, or still-pending outcomes remain errors. Explicit relay protocol or overload rejections remain errors. This confirmation uses the selected workspace and never reconnects or launches another conversation.

## Streaming agents

Subclass `StreamingVoiceAgent` when ASR, LLM, and TTS work runs independently of the socket receive callback. `on_ready()` starts your pipeline and returns promptly; `on_audio()` enqueues incoming audio. `audio_output()` yields audio whenever it becomes available, including greetings and speech during input silence. The SDK paces output and propagates producer failures. `close()` must cancel and await your pipeline and finish its conversation trace.

```python
import asyncio
import coval_voice


class MyAgent(coval_voice.StreamingVoiceAgent):
    def __init__(self, simulation_id, pipeline):
        self.pipeline = pipeline
        self.replies = asyncio.Queue(maxsize=100)
        # Connect this conversation's pipeline output to this bounded queue.

    async def on_ready(self):
        await self.pipeline.start()

    async def on_audio(self, audio):
        await self.pipeline.enqueue_audio(audio)

    async def audio_output(self):
        while True:
            yield await self.replies.get()

    async def close(self):
        await self.pipeline.close()
```

The pipeline methods above represent your agent's integration points. The Agent Playground `agents/pipecat-sdk` example implements the adapter with a real Pipecat pipeline. Keep output queues bounded, yield even-length PCM16 `bytes`, and limit each chunk to 2 MiB. Yielding normal 20 ms chunks avoids holding a long reply in memory. An output iterator that ends before Coval sends `session_end` fails the session. Streaming output begins only after `on_ready()` finishes.

The existing callback adapter remains supported and may return reply bytes from `on_ready()` and `on_audio()`. Use the streaming interface when background speech must not wait for the next input callback. The old `coval_tunnel` imports and `coval-tunnel` CLI remain compatible.

This release does not add relay clear/barge-in controls. Adapters must not claim interruption parity with ordinary Coval WebSocket agents.

## Upload Pipecat traces

Trace upload is optional. Create a `CovalTraceExporter`, pass it to Pipecat's `setup_tracing(exporter=...)`, and pass the same object to `CovalVoiceClient`. Configure each Pipecat `PipelineWorker` with `conversation_id=simulation_id`, using the ID passed to the agent factory. When a voice session ends, the client flushes ended spans and posts only that conversation's trace to `POST /v1/traces` with its simulation ID. For example:

```python
import coval_voice
from pipecat.utils.tracing import setup

trace_exporter = coval_voice.CovalTraceExporter()
setup.setup_tracing(service_name="my-agent", exporter=trace_exporter)
client = coval_voice.CovalVoiceClient(
    api_key=api_key, trace_exporter=trace_exporter
)
# In create_agent(simulation_id), set PipelineWorker(conversation_id=simulation_id).
```

Pipecat spans can contain transcripts, prompts, and model output. Enable tracing only when those fields are approved for Coval. The buffer holds up to 10,000 ended spans by default; tune `max_buffered_spans` for your process. Trace metadata has a separate `max_buffered_traces` limit of 10,000, including damaged traces retained to prevent incomplete uploads. Reducing the span budget does not reduce this metadata budget.

Failed sessions close the agent, flush ended spans, and discard that conversation's trace. If a failed agent never exports its conversation root, metadata pressure reclaims the oldest child-only or damaged trace. Healthy identified traces and in-flight uploads are retained. A start-time watermark rejects delayed roots from discarded traces, so reclaimed metadata cannot turn an incomplete trace into a successful upload. Completed uploads that fail remain buffered for an explicit retry. Cleanup failures retain the original session or agent error and attach secondary trace errors as exception notes. The agent's `close()` method must await PipelineWorker completion so its conversation span has ended before the SDK flushes and uploads it. The CLI does not enable trace export automatically.

Trace flush or upload failures after a completed voice session are reported on stderr without failing the session or cancelling sibling calls. Retained spans can be uploaded explicitly through the exporter once the trace endpoint recovers. Do not rerun a completed conversation to retry telemetry. Agent and voice-session failures still propagate normally.

The SDK probes its relay connection every 20 seconds and allows 20 seconds for a pong. A connection that stops responding fails visibly instead of waiting indefinitely. No automatic replay occurs after a claimed session disconnects.

## Connect one existing simulation

Set `COVAL_API_KEY` to an API key for the target organization/workspace. Pass `workspace_id` to the client (CLI `--workspace-id` or `COVAL_WORKSPACE_ID`) to select a custom workspace explicitly. The selector is sent on every API request and trace upload; server-side authorization still applies. The API base URL defaults to `https://api.coval.dev/v1`. For the local stack, pass `--api-base-url http://127.0.0.1:8081/v1`.

```bash
PYTHONPATH=/path/to/your/agent \
  coval-tunnel \
  --agent-module your_agent_module \
  connect SIMULATION_ID
```

The CLI agent module must expose the zero-argument `create_agent()` factory. This CLI convention is separate from the importable client's `create_agent(simulation_id)` factory described above. A fresh instance is constructed for every conversation. It must implement async `on_ready()`, `on_audio(bytes)`, and `close()` methods. `on_ready` and `on_audio` may return PCM16 mono 16 kHz bytes to send to Coval, or `None` when they have no audio to send.

## Launch and attach to a run

```bash
PYTHONPATH=/path/to/your/agent \
  coval-tunnel \
  --agent-module your_agent_module \
  run --agent-id AGENT_ID --persona-id PERSONA_ID --test-set-id TEST_SET_ID
```

For a developer worker using the shared dev queue, pass `--dev-id YOUR_DEV_ID` to the `run` command or set `COVAL_DEV_ID`. The value is included as `dev_id` in `POST /v1/runs`; it must match the ID configured on the local pipeline worker. A command-line value takes precedence over the environment variable.

Run-monitoring reads retry temporary HTTP and network failures for up to 30 seconds per read, capped by the run's original deadline. Authentication and other permanent errors stop immediately. Launch requests are never retried automatically, so a retry cannot create a second run.

The command posts `/v1/runs`, lists simulations using the returned `run_id`, and attaches a separate local agent to each conversation created by that run. It reports an error if a conversation finishes before attachment. It polls each simulation's `/connect` resource until the worker publishes a short-lived WSS offer. The token is sent only in the WebSocket upgrade's `Authorization` header. The worker and SDK each use a distinct signed role credential and the relay pairs only sockets for the same simulation attempt. The client waits for `session_ready`, then exchanges binary PCM audio and JSON control messages. Outgoing audio is paced in 20 ms chunks.

## Limits

The executable assumes the backend and agent agree on PCM16 little-endian, one channel, 16 kHz. It does not resample an arbitrary agent's audio. A socket close ends that conversation; a new simulation attempt must obtain a new offer and token.

Transient WebSocket upgrade failures can retry a live offer before its deadline. Once the authenticated SDK join consumes the one-use token, a socket failure ends that attempt, including a failure before `session_ready`. Recovery requires a new simulation attempt and fresh role credentials. The SDK does not rewind or resume a claimed attempt.

Only one caller can obtain an unclaimed join token for an attempt. Concurrent discovery receives a retryable conflict while that token is valid. This preserves the first caller's credential. If that caller disappears before attachment, the token expires within 60 seconds and discovery can issue a new one.

SQS redelivery does not resume an in-progress voice simulation. The shared simulator claims only queued outputs, so duplicate deliveries cannot replace an active call. Use a new run to start a fresh call after a worker crash. A connected worker holds a 60-second ownership lease and renews it every 20 seconds. Resimulation cannot replace that attempt while its lease is live. After a worker dies and its lease expires, a queued rerun can create a fresh attempt. An unattached `PROVISIONING` or `READY` offer can be replaced after its connection deadline. Queued reruns report `PENDING` until setup starts their execution, even if they retain an old end timestamp or relay record. Once the simulation has a saved terminal outcome, discovery returns that outcome even if the relay's terminal DynamoDB write failed. Completed simulations return `CLOSED`, cancelled simulations return `CANCELLED`, and other ended simulations return `FAILED`, without issuing another join token.
