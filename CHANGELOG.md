# Changelog

User-facing changes are recorded here. This project is in pre-release development; no stable release is listed.

## Unreleased

### Added

- A Python client for launching or attaching voice agents to Coval simulations with explicit workspace selection.
- Callback and independent streaming adapters, paced PCM16 audio, bounded buffering, deadline handling, and conversation cleanup.
- Optional per-conversation OpenTelemetry buffering and trace upload.
- A Python-only API with immutable run/session/trace results, partial run snapshots on SDK run failures, and no internal logging or console output.
- Typed package distributions, isolated-wheel validation, and supported-Python CI.
- Google-based Python conventions, Ruff, strict Pyright, architecture checks, contribution guidance, and Apache-2.0 licensing.
- Optional OpenTelemetry dependencies through `[tracing]`, with a dependency-free core trace contract and the concrete exporter in `coval_agent_sdk.tracing`.
- Internal use of `coval-sdk` behind a private HTTP adapter, with no public transport client or generated API types.
- A shared `CovalError` base for SDK failures, preserving original adapter exceptions and cancellation through run/session cleanup.
- Consistent callback/stream PCM validation and pacing, readiness-ordered completion, and preservation of output errors through iterator cleanup.
- Silent, single-attempt OTLP protobuf delivery through the private Coval transport.
- Batch attachment defers queued conversations until they start, preserving the batch deadline and rejecting unattached outcomes; permanent relay upgrades fail without retrying.
