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
