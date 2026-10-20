# Developer API and Ergonomics

The public boundary is a conversation audio connection. Applications and frameworks retain their own agent classes, construction, configuration, state, tasks, and provider resources. The SDK supplies no agent base class, protocol, factory, compatibility alias, or executable entry point.

## Developer journey

Install the SDK, select an environment/workspace and existing Coval fixtures, then call `await client.run(config, on_connection=handle_connection)`. The async handler receives one ready `AudioConnection`, connects it to the application's pipeline, stays active through voice completion, and releases application resources before returning. It returns no agent object. Multiple handlers may run concurrently, each with its own simulation ID and audio connection; applications own their framework's state isolation.

For an existing simulation, use `async with client.connect(simulation_id) as connection`. The same pipeline integration works inside this context. Read `connection.result` after successful context exit. No run is created. Early normal exit is a failure, and context cleanup does not imply hosted cancellation.

## Audio interface

`AudioConnection` exposes `simulation_id`, `receive_audio()`, `send_audio(bytes)`, `wait_closed()`, `closed`, and `result`. Receive and send are independent. There is one active receive iterator; sends are serialized, paced, validated, and awaited. Input is bounded by bytes. The format is fixed PCM16 little-endian mono 16 kHz. See [runtime contracts](runtime.md) for exact completion, buffering, partial-write and failure semantics.

Framework plugins use this connection as an input/output transport. They may live in consumer projects; the core never imports their dependencies. The Playground Pipecat transport demonstrates this boundary without inheriting any SDK agent class.

## Results and failures

`run()` returns immutable `RunResult` with `run_id`, hosted `status`, and `conversations: tuple[SessionResult, ...]`. Successful completion requires a completed hosted run and all observed conversations attached and completed locally. Execution completion does not mean quality criteria passed.

After a successful connection context, `connection.result` is a `SessionResult` with `simulation_id`, local voice `status`, PCM byte counts, and optional `trace: TraceResult`. Disabled tracing is None; delivery failures retain structured diagnostics without failing completed voice. Results contain no dependency clients, credentials or transport objects.

After launch, SDK run failures raise `RunError` with a partial result and last observed hosted status. Application exceptions and cancellation propagate unchanged after local cleanup. All SDK-owned errors derive from `CovalError`; they do not contain generated API SDK response types. The application owns presentation; the SDK emits no logs or console output.

## Distribution and optional integrations

The distribution is `coval-agent-sdk`, the Python namespace is `coval_agent_sdk`, and the package is unreleased. Update consumers alongside intentional API changes without aliases or deprecation paths. OpenTelemetry is available only through `[tracing]`, with its concrete exporter in `coval_agent_sdk.tracing`. Publishing and visibility changes follow [release preparation](releasing.md) and require separate authorization.
