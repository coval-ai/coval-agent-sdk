# Architecture

The SDK owns the connection between a local voice agent and a Coval simulation. It owns authentication, connection discovery, audio transport, pacing, run monitoring, and terminal cleanup. The application owns its agent framework, provider clients, state isolation, pipeline queues, and background tasks.

## Module ownership

| Module | Owns |
| --- | --- |
| `src/coval_agent_sdk/__init__.py` | The explicit supported public API and its `__all__`. |
| `audio.py` | Public per-conversation audio connection, bounded input, and observable completion. |
| `results.py` | Immutable run, conversation, and trace outcomes. |
| `config.py` | Immutable run configuration and its validation. |
| `errors.py` | API and relay errors, including partial run outcomes exposed to callers. |
| `client.py` | Public run handler and connection context entry points. |
| `telemetry.py` | Dependency-free trace lifecycle protocol used by the client. |
| `tracing.py` | Optional OpenTelemetry provider flush, buffers, and upload lifecycle. |
| `_http.py` | Private Coval API SDK adapter, JSON validation, error translation, and request resource cleanup; mutation requests are not retried. |
| `_connection.py` | Offer discovery and one-use WebSocket connection setup. |
| `_audio.py` | Private connection state shared by the public handle/context, serialized PCM pacing, validation, and byte accounting. |
| `_session.py` | Connection context, relay reader, readiness, input acknowledgements, handler observation, and trace cleanup. |
| `_runs.py` | Run launch, paginated conversation discovery, monitoring, and sibling cancellation. |
| `_defaults.py` | Internal protocol limits and timeout defaults. |
| `_exceptions.py` | Secondary cleanup diagnostics that preserve primary exception identity. |

Paths without a directory prefix in this table are relative to `src/coval_agent_sdk/`. Modules beginning with `_` are implementation details; import the documented API from `coval_agent_sdk` in a consumer.

## Dependency direction

Public client methods call run/session orchestration. Orchestration depends on connection, audio, and HTTP owners. Configuration, results, errors, and defaults remain small leaf responsibilities. The public audio connection owns its input buffer and write coordination. Core orchestration depends on the dependency-free `TraceExporter` protocol. Only the optional `tracing` module imports OpenTelemetry; it owns provider flushing, buffers, and OTLP protobuf encoding and does not import session orchestration. Import its concrete exporter from `coval_agent_sdk.tracing`, not the root facade. The architecture checker rejects OpenTelemetry imports outside that integration module.

Internal code imports owning modules rather than the root public facade. The root initializer is the only symbol-reexport facade. Do not introduce a cycle or move an import inside a function to hide one. `scripts/check_architecture.py` checks the allowed first-party dependency graph, top-level absolute imports, production dependency roots, and initializer contents.

A new module requires an explicit dependency policy in that checker. Choose its responsibility from the behavior and state it owns; the policy is not a reason to split one operation into forwarding functions. Import conventions and size/complexity review signals live in [Python conventions](python-style.md).

`coval-sdk` supplies authentication, serialization, and HTTP requests and supports Python 3.9 and newer, within this package's Python 3.10 minimum. It is MIT-licensed and installed as a dependency rather than vendored. The direct `urllib3` dependency declares the network exception types the adapter translates; it is also MIT-licensed.

Only `_http.py` imports `coval_sdk` and `urllib3`; the architecture checker enforces this boundary. The public client owns a private `_api` adapter. Public signatures, results, attributes, and exports contain no API SDK objects or types. Request failures become agent SDK errors rather than passing dependency exceptions to applications.

Tracing sends encoded protobuf through the same private adapter as JSON API requests, without the OpenTelemetry HTTP exporter's logging or automatic retry policy. The adapter uses the API SDK's request serialization and client methods because connection discovery is absent from its generated endpoint set. It validates JSON objects before orchestration consumes them. Each blocking request owns its API client and connection pool, which is cleared in the worker thread after success or failure. Cancellation stops waiting on that worker; the worker still releases its resources when the request finishes or times out. No shared SDK defaults or debug logging settings are modified.

## State and resources

A client holds API configuration and an optional trace exporter. Each connection context owns a relay reader and AudioConnection. The connection owns its bounded incoming buffer, readiness/completion events, and audio byte counts. The private PCM writer owns a send lock and cross-call pacing. The SDK never creates or closes an application agent. Application handlers release pipeline resources before the SDK finalizes traces.

Run monitoring records every created task before continuing discovery, so a later-page failure can still cancel and await all active local sessions. It tracks conversations that ended before attachment rather than silently reporting a partial run as complete. Cancelling local tasks does not imply the hosted run was cancelled.

The trace exporter is process-wide but correlates and uploads one conversation at a time. Its lock protects buffer accounting and upload ownership; network calls happen outside the lock. [Runtime contracts](runtime.md) describe the failure paths reviewers must preserve.

## Extension boundaries

Keep provider-specific models, agent frameworks, resampling, and application policy in framework transports or consumer projects. Add a core abstraction only when the SDK owns a demonstrated shared contract. A framework integration does not justify making every SDK installation depend on that framework.

Treat public exports as deliberate API choices and keep implementation helpers private. Document supported behavior in [Developer API](developer-api.md). Describe proposed interface changes in an issue before implementation, including their effect on existing integrations and release versioning.

## Application output

The SDK returns structured outcomes and raises failures. It does not print, log, configure logging, or ship executable package entry points. Applications own presentation, including any launcher they provide. Trace-upload diagnostics belong in conversation results.
