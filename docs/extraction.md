# Extraction Provenance

The starting implementation is [backend PR #8227](https://github.com/coval-ai/backend/pull/8227) at commit `3e045984e15634ef40d85dcd02489d1c697e7d3c`, under `sdk/python/coval-voice-tunnel/`.

The `coval_voice` package, its type marker, both compatibility modules, all three existing test files, and the Python type-check configuration are copied without runtime changes. The [manifest](extraction-manifest.json) records original tracked-file hashes. README and packaging metadata are adapted for the standalone directory; package version, dependency constraints, console command, and public imports are retained.

The backend publishing workflow is deliberately excluded. Its `RELEASING.md` is replaced by local installation/build instructions. The standalone source is hosted in the private `coval-ai/coval-agent-sdk` repository, with a README-only initial commit and a draft implementation PR. No package release or publishing configuration is included. The original backend and playground branches are preserved while design work proceeds here.

The repository name is `coval-agent-sdk`; the existing distribution name is `coval-voice-tunnel`; Python imports use `coval_voice`. These are different names with different consumers. Consolidating the developer-facing naming is a design question rather than an incidental extraction edit.

Covering work: [COVAL-7086 — Package the Python Voice Agent SDK for streaming agents](https://linear.app/coval/issue/COVAL-7086/package-the-python-voice-agent-sdk-for-streaming-agents). Playground integration remains under COVAL-7087.
