# Developer API and Ergonomics

Status: design proposal. The extraction preserves the existing executable API. New names, result objects, and context objects below are sketches, not implemented interfaces.

## The Developer Journey

A developer already has a voice agent. They should install one local SDK, select a Coval workspace and test fixtures, provide a per-conversation agent factory, and run a simulation. Coval owns the simulated caller and evaluation; the developer's process owns their agent. Each conversation gets its own adapter and lifecycle. The SDK owns outbound connectivity, authentication, discovery, audio pacing, and terminal cleanup.

Keep these responsibilities distinct: the SDK should not require an agent framework or a backend checkout, and an adapter should not implement relay authentication, retries, or run discovery. Package installation and deterministic validation should need no live credentials.

## Current Executable API

```python
import asyncio
import os

from coval_voice import CovalVoiceClient, RunConfig
from my_agent import create_agent

async def main():
    client = CovalVoiceClient(
        api_key=os.environ["COVAL_API_KEY"],
        workspace_id=os.environ["COVAL_WORKSPACE_ID"],
        api_base_url="https://api.coval.dev/v1-staging",
    )
    await client.run(
        RunConfig(
            agent_id="AGENT_ID",
            persona_id="PERSONA_ID",
            test_set_id="TEST_SET_ID",
            run_timeout_seconds=600,
        ),
        create_agent,
    )

asyncio.run(main())
```

`my_agent` is the developer's own adapter module. In the library API its factory receives `simulation_id` and returns a fresh callback or streaming agent. The generic CLI currently expects a zero-argument `create_agent()`. Both `run()` and `connect()` return `None`; run identity and completion information are printed. The default API endpoint is production, and workspace selection is optional in the client, so examples should select staging and an explicit workspace while developing.

The streaming adapter implements `on_ready()`, `on_audio(bytes)`, `audio_output()`, and `close()`. Its callbacks enqueue promptly, its output is an independent async stream, and it owns cancellation of its own provider tasks. Audio is PCM16 little-endian, mono, 16 kHz. The SDK does not resample arbitrary adapter output or implement relay interruption/clear controls.

## First Design Pass

### 1. Use One Factory Contract Everywhere

The CLI and Python API should construct agents with the same information. Candidate shape:

```python
def create_agent(session: VoiceSession) -> StreamingVoiceAgent:
    return MyAgent(conversation_id=session.simulation_id)
```

The proposed `VoiceSession` describes the simulation identity and negotiated audio format. It contains no relay tokens or SDK transport objects. The context is immutable and per conversation; provider-specific setup stays in the adapter. Decide whether the typed context earns its extra concept over the current simulation-ID string before implementing it. Existing factories would need a deliberate compatibility path.

### 2. Return a Useful Result

Keep the current `RunConfig` shape while making completion inspectable in code:

```python
result = await client.run(config, create_agent)
print(result.run_id, result.status)
for conversation in result.conversations:
    print(conversation.simulation_id, conversation.status)
```

The proposed result describes persisted execution outcomes and identifiers. Metric values are separate from successful evaluation execution. An evaluator finishing does not mean the agent passed its quality criteria. Trace-export warnings should be distinct from voice-session failure, and local cancellation must not imply the hosted run was cancelled. Decide how a failed batch exposes partial results alongside a typed error rather than silently returning success.

### 3. Make Setup and Failures Legible

Use the standalone project's name consistently in documentation and make an explicit naming decision for the eventual distribution, import namespace, and console command. Avoid adding a second client class just to shorten its name. Make the selected environment and workspace clear at the entry point; do not silently change the current production default during extraction.

Separate fixture/configuration mistakes, authentication failures, provider errors, transport/backpressure failures, timeouts, and telemetry warnings in the developer-facing result/error contract. Keep run and simulation IDs available for debugging. Never include credentials or one-use relay offers in result objects or normal diagnostics.

## Order of Iteration

First settle the smallest first-run example, factory contract, and result shape. Implement the agreed contract with focused lifecycle/compatibility tests. Then update the Pipecat consumer in agent-playground PR #93 and exercise it through an editable install for iteration and a built wheel for acceptance. The standalone source is hosted in the private `coval-ai/coval-agent-sdk` repository. Package-registry publication remains deferred; the implementation and design stay in a draft PR during iteration.

Before retiring the SDK files in backend PR #8227, settle ownership of the extracted package and its validation workflow. Server-side API, worker, relay, and infrastructure code remain with their owning services.
