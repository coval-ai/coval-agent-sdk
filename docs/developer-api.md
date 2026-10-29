# Developer API

Integrate an existing voice pipeline through `AudioConnection`. Your application owns pipeline construction, conversation state, provider resources, and cleanup.

## Developer journey

Install the SDK, select an environment/workspace and existing Coval fixtures, then call `await client.run(config, on_connection=handle_connection)`. The async handler receives one ready `AudioConnection`, connects it to the application's pipeline, stays active through voice completion, and releases application resources before returning. Handlers may run concurrently, each with its own simulation ID and audio connection; isolate their state in your application.

For an existing simulation, use `async with client.connect(simulation_id) as connection`. The same pipeline integration works inside this context. Read `connection.result` after successful context exit. No run is created. Early normal exit is a failure, and context cleanup does not imply hosted cancellation.

## Audio interface

`AudioConnection` exposes `simulation_id`, `receive_audio()`, `send_audio(bytes)`, `wait_closed()`, `closed`, and `result`. Receive and send are independent. There is one active receive iterator; sends are serialized, paced, validated, and awaited. Input is bounded by bytes. The format is fixed PCM16 little-endian mono 16 kHz. See [runtime contracts](runtime.md) for exact completion, buffering, partial-write and failure semantics.

Framework plugins use this connection as an input/output transport. They may live in consumer projects; the core never imports their dependencies. For the integration workflow, see the [Agent SDK guide](https://docs.coval.ai/sdks/agent-sdk).

## Results and failures

`run()` returns immutable `RunResult` with `run_id`, hosted `status`, `conversations: tuple[ConversationResult, ...]`, and `failures: tuple[ConversationFailure, ...]` (empty on success). Successful completion requires a completed hosted run and all observed conversations attached and completed locally. Execution completion does not mean quality criteria passed.

After a successful connection context, `connection.result` is a `ConversationResult` with `simulation_id`, local voice `status`, PCM byte counts, and optional `trace: TraceResult`. Disabled tracing is None; delivery failures retain structured diagnostics without failing completed voice. Results contain no dependency clients, credentials or transport objects.

An SDK session failure does not stop unrelated or queued conversations. Once the batch finishes, it raises `RunError`; `error.result.conversations` contains successful sessions and `error.result.failures` retains each failed simulation ID, SDK exception `error_type`, and diagnostic `message`. A hosted COMPLETED status cannot override a local transport failure. Global monitoring failures, session authentication/authorization failures and the batch deadline still stop local work promptly. After launch, SDK run failures retain the last observed hosted status in the error result. Application exceptions and cancellation propagate unchanged after local cleanup. All SDK-owned errors derive from `CovalError`; they do not contain generated API SDK response types. The application owns presentation; the SDK emits no logs or console output.

## Distribution and optional integrations

Install `coval-agent-sdk` from PyPI and import `coval_agent_sdk`. Install `coval-agent-sdk[tracing]` for the OpenTelemetry exporter in `coval_agent_sdk.tracing`. See the changelog before upgrading and [releasing](releasing.md) for maintainer workflow.

## Argument validation

Python rejects missing required arguments and unknown keywords with `TypeError`. `CovalVoiceClient` constructor arguments are keyword-only. `RunConfig` validates nonempty required identifiers, positive iterations/concurrency, and a positive finite deadline during construction, raising `ValueError` for those invalid values. The client rejects API roots whose scheme is neither HTTP nor HTTPS. These local checks run before any network request.

Type annotations do not provide general runtime type validation. Current value checks do not require identifiers to be strings or iterations/concurrency to be integers; some incorrect types pass construction, while others produce Python errors when used. Client construction does not reject an empty API key or verify that credentials and fixture identifiers exist or belong to the selected workspace. Those checks belong to the server and failures are reported through SDK HTTP errors when a request is made. Result dataclasses are immutable but do not validate their annotated field types at construction.

`AudioConnection` objects supplied by the client are ready to use. Constructing one directly only creates local state; sending before readiness raises `CovalError`. Audio writes validate the bytes/PCM-size contract when awaited. Application handler errors and cancellation propagate unchanged after cleanup. See [runtime contracts](runtime.md) for connection and failure behavior.
