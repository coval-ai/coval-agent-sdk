# Architecture

The SDK owns the connection between a local voice agent and a Coval simulation. It owns authentication, connection discovery, audio transport, pacing, run monitoring, and terminal cleanup. The adapter owns its agent framework, provider clients, input/output queues, and background tasks.

## Module ownership

| Module | Owns |
| --- | --- |
| `src/coval_agent_sdk/__init__.py` | The explicit supported public API and its `__all__`. |
| `agent.py` | Callback protocol, streaming abstract base class, and per-conversation factory type. |
| `results.py` | Immutable run, session, and trace outcomes. |
| `config.py` | Immutable run configuration and its validation. |
| `errors.py` | API and relay errors, including partial run outcomes exposed to callers. |
| `client.py` | Public `run()` and `connect()` orchestration. |
| `tracing.py` | Bounded conversation trace buffers and upload lifecycle. |
| `_http.py` | Authenticated JSON requests; mutation requests are not retried. |
| `_connection.py` | Offer discovery and one-use WebSocket connection setup. |
| `_audio.py` | Outgoing PCM pacing, reply budgets, and streaming producer ownership. |
| `_session.py` | One agent/socket session, readiness, input acknowledgements, and cleanup. |
| `_runs.py` | Run launch, paginated conversation discovery, monitoring, and sibling cancellation. |
| `_defaults.py` | Internal protocol limits and timeout defaults. |

Paths without a directory prefix in this table are relative to `src/coval_agent_sdk/`. Modules beginning with `_` are implementation details; import the documented API from `coval_agent_sdk` in a consumer.

## Dependency direction

Public client methods call run/session orchestration. Orchestration depends on connection, audio, and HTTP owners. Adapter contracts, configuration, errors, and defaults remain small leaf responsibilities. Tracing owns its own buffers and HTTP exporter and does not import session orchestration.

Internal code imports owning modules rather than the root public facade. The root initializer is the only symbol-reexport facade. Do not introduce a cycle or move an import inside a function to hide one. `scripts/check_architecture.py` checks the allowed first-party dependency graph, top-level absolute imports, production dependency roots, and initializer contents.

A new module requires an explicit dependency policy in that checker. Choose its responsibility from the behavior and state it owns; the policy is not a reason to split one operation into forwarding functions. Import conventions and size/complexity review signals live in [Python conventions](python-style.md).

## State and resources

A client holds API configuration and an optional trace exporter. Each `connect()` call or discovered run conversation creates a fresh session and adapter. The session owns its output task, socket context, readiness state, input byte count, and terminal outcome. Outgoing audio owns its bounded callback queue, in-flight byte accounting, and producer iteration.

Run monitoring records every created task before continuing discovery, so a later-page failure can still cancel and await all active local sessions. It tracks conversations that ended before attachment rather than silently reporting a partial run as complete. Cancelling local tasks does not imply the hosted run was cancelled.

The trace exporter is process-wide but correlates and uploads one conversation at a time. Its lock protects buffer accounting and upload ownership; network calls happen outside the lock. [Runtime contracts](runtime.md) describe the failure paths reviewers must preserve.

## Extension boundaries

Keep provider-specific models, agent frameworks, resampling, and application policy in adapters or consumer projects. Add a core abstraction only when the SDK owns a demonstrated shared contract. A framework integration does not justify making every SDK installation depend on that framework.

Treat public exports and entry points as deliberate API choices. This pre-release package has no reason to ship alternate import names for unexposed implementation helpers. Proposed interface changes belong in [API design](developer-api.md) until their behavior and migration implications are settled.

## Application output

The SDK returns structured outcomes and raises failures. It does not print, log, configure logging, or ship executable package entry points. Applications own presentation, including any launcher they provide. Trace-upload diagnostics belong in session results rather than console warnings.
