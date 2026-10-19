# Developer API and Ergonomics

The README and public docstrings describe the implemented API. Factory context objects below remain proposals; the result objects described here are implemented.

## Developer journey

A developer installs the SDK, selects an environment and workspace, provides existing agent/persona/test-set fixture IDs, and passes a per-conversation factory to `CovalVoiceClient.run()`. Use `connect()` to attach to an existing simulation. The factory receives `simulation_id` and returns a fresh callback or streaming agent. Provider configuration belongs in the adapter.

The SDK is a Python library with no CLI, module execution entry point, internal logging, or console output. It returns results and raises failures; the application owns presentation. The Agent Playground has its own launcher using this Python API.

## Implemented results

`run()` returns an immutable `RunResult` with `run_id`, the last hosted `status`, and `conversations: tuple[SessionResult, ...]`. Successful completion requires the hosted run to be `COMPLETED` and every eligible conversation to have attached and finished locally. Execution completion does not mean quality criteria passed.

`connect()` returns an immutable `SessionResult` with `simulation_id`, local voice `status`, received/sent PCM byte counts, and optional `trace: TraceResult`. Voice completion does not imply hosted evaluation completion. A trace result reports `uploaded_spans` (unknown/`None` after a failure) and an optional flush/upload `error`; disabled tracing is represented by `None`. Telemetry failures do not turn a completed conversation into a failed voice session.

After run creation, an SDK run failure raises `RunError`. Its `result` carries the run ID, last observed monitoring status (`None` before the first status response), and completed local conversations after cleanup. The hosted status may still indicate work in progress; the SDK does not invent a hosted cancellation outcome. Failures before run creation and cancellation propagate without fabricating a run result.

## Remaining factory context proposal

The current factory receives a simulation-ID string. A possible future immutable context could also describe audio format:

```python
def create_agent(session: VoiceSession) -> StreamingVoiceAgent:
    return MyAgent(conversation_id=session.simulation_id)
```

`VoiceSession` is not implemented. Decide whether a context earns its extra concept before adding it. It would contain no relay tokens or transport objects. The removed SDK CLI does not introduce a second factory contract to support.

## Design boundaries

The distribution is `coval-agent-sdk` and imports use `coval_agent_sdk`. This unreleased SDK has no backward-compatibility obligations: update consumers alongside intentional API changes without aliases or deprecation paths. Keep provider frameworks, backend configuration, and application policy outside the package.

OpenTelemetry is an opt-in `[tracing]` extra; the concrete exporter lives in `coval_agent_sdk.tracing`. Core annotations use the dependency-free `TraceExporter` protocol. All SDK-owned errors derive from `CovalError`; adapter exceptions and cancellation remain outside that hierarchy. Environment defaults remain a separate design decision. Server-side API, worker, relay, and infrastructure behavior is outside this package. Release preparation is documented in [releasing](releasing.md).
