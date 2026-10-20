# Contributing

The [README](README.md) introduces the SDK and its supported API. This guide describes how to make a reviewable change; [Python conventions](docs/python-style.md) define the coding standard.

## Set up the project

Use Python 3.12 and [uv](https://docs.astral.sh/uv/) for development. The package supports Python 3.10 and newer; CI exercises 3.10, 3.12, and 3.14.

```sh
git clone https://github.com/coval-ai/coval-agent-sdk.git
cd coval-agent-sdk
scripts/install
.venv/bin/python -c "import coval_agent_sdk"
```

The editable installation makes source changes available in `.venv`. Installation, imports, static checks, and tests do not need Coval or model-provider credentials. Use `PYTHON_VERSION=3.10 scripts/install` in a fresh checkout to create a development environment for another supported interpreter.

## Pull requests

1. Start a focused branch from `main`. For a substantial API change, describe the proposed behavior in an issue before adding another abstraction.
2. Identify the owning module using [architecture](docs/architecture.md). Keep provider integrations in adapters outside the core package.
3. Implement the change and add behavior-focused coverage for affected contracts, including relevant failure and cancellation paths.
4. Run the checks below and describe any unavailable or unrelated failing check accurately.
5. Open a pull request explaining the problem, resulting behavior, relevant evidence, and any migration or release implications. Link the issue when one exists.
6. Have the change reviewed before merging. Keep design experiments in draft until their scope and API are ready for review.

Use a concise Conventional Commit subject such as `fix: preserve cancellation during trace cleanup` or `docs: clarify streaming adapter ownership`. Commit and PR bodies explain why the change is needed; they should not narrate every command run.

## Checks

```sh
scripts/lint
scripts/check
```

`scripts/lint` runs Ruff lint, Ruff formatting checks, the architecture policy, and strict Pyright checks for production code. `scripts/check` also builds a source distribution, builds its wheel, checks metadata, installs the wheel into an isolated environment, and runs the SDK's tests outside the checkout. CI uses the same entry points.

For a focused test during iteration:

```sh
.venv/bin/python -m pytest tests/test_streaming.py -q
```

For safe automated fixes:

```sh
.venv/bin/ruff check --fix .
.venv/bin/ruff format .
```

Review generated edits before committing. Run `scripts/check` before pushing runtime, dependency, entry-point, or packaging changes. For a prose-only edit, verify links and commands that changed and run `git diff --check`; do not rerun unrelated runtime tests merely to change a paragraph.

## Tests

Assert on observable behavior: the audio or requests sent, returned results, typed failures, resource state, and cancellation or cleanup outcomes. Keep the owner under test real, and substitute only external boundaries needed for a deterministic test. Use populated values and otherwise-valid inputs so a rejection case fails for the intended reason.

Patch collaborators at their owning modules, not through the root facade. Keep tests under `tests/`; do not insert the checkout into `sys.path`. Packaging and public API tests must also pass against an installed wheel. Preserve each distinct behavior when splitting or consolidating cases, and never hide an affected failure with a skip, weaker assertion, or broader mock.

Use synthetic identifiers and audio. Do not make live API or provider calls in the automated suite. A separately authorized hosted check must record the selected environment, source/artifact identity, and terminal result; mocked checks do not establish hosted acceptance.

## API and dependencies

Add public exports deliberately and document their ownership, inputs, outputs, error behavior, and cleanup obligations. Keep the importable package independent of any particular agent framework. Put unimplemented interfaces in [API design](docs/developer-api.md), clearly marked as proposals.

Expose conversation audio connections without agent classes or factories. Applications own agent/pipeline construction and cleanup; SDK connection contexts and async run handlers own only audio transport and simulation orchestration. Framework plugins belong in consumers, and application cleanup must finish before trace finalization.

Keep the SDK a Python library only. Return structured results and raise failures without printing, emitting log records, configuring logging, or adding console-script or package-execution entry points. Calling applications own presentation. Preserve useful diagnostics in returned outcomes and errors as described in [runtime contracts](docs/runtime.md#returned-outcomes); test that affected public operations return the expected information without writing console output or log records.

Keep `coval-sdk` and its HTTP dependency inside the private `_http.py` adapter. Do not expose dependency clients, configuration, models, raw responses, or exception types through public attributes, parameters, return values, or exports. Translate request failures into this package's errors at that boundary; orchestration owns read retries, and run creation must remain a single attempt. Do not enable dependency debug output or change application logging configuration.

Derive SDK-owned failures from `CovalError` and retain useful specific subclasses. Do not wrap application exceptions or cancellation as SDK errors. Preserve the original failure through session cleanup and batch sibling cancellation; attach `RunError` snapshots only to SDK failures after launch. Completed-session trace delivery failures remain structured diagnostics.

Put optional integration dependencies in a named extra and declare every external package imported directly by that integration. Keep the base SDK importable and runnable without those dependencies. OpenTelemetry belongs only in `coval_agent_sdk.tracing`; use its protobuf encoder with the private HTTP adapter rather than an exporter that emits logs or hides retry policy. core code uses the dependency-free `TraceExporter` contract. `scripts/check` first exercises a clean base wheel installation, then installs `[tracing]` and runs the full suite.

For a new dependency, explain why the SDK needs it, its Python support, and its distribution/license implications. Update the affected tests and package metadata. Review dependency upgrades as code changes; do not relax constraints solely to silence a failing check.

Update [CHANGELOG.md](CHANGELOG.md) under `Unreleased` when user-facing behavior, supported APIs, dependencies, or packaging changes. This is a pre-release SDK; do not invent a released compatibility promise for an internal helper.

## Where prose lives

- `README.md` introduces the SDK, installation, and a complete first-run example.
- `AGENTS.md` contains concise coding-agent instructions and links to their explanations.
- `AGENTS.meta.md` governs changes to the agent instructions.
- `CONTRIBUTING.md` describes the human contribution and review workflow.
- `docs/python-style.md` defines imports, initializers, types, docstrings, and formatting.
- `docs/architecture.md` maps responsibilities and dependency direction.
- `docs/runtime.md` explains audio, connection, timeout, cancellation, and trace contracts.
- `docs/local-development.md` explains editable installs, built-wheel checks, and consumer development.
- `docs/releasing.md` describes maintainer release preparation and publication boundaries.
- `SECURITY.md` describes private vulnerability reporting.

Keep explanations in their owning document and link to them elsewhere. Use one paragraph per line in Markdown; leave code blocks, tables, and lists structured normally. Python comments and docstrings follow the formatter and 80-column conventions instead.

## Comments and docstrings

Use names, types, and small functions to explain what the code does. Write a comment when it adds a constraint, rationale, external contract, or concurrency consequence the code cannot express clearly. Keep it next to the code it explains, and link durable documentation when that settles the point.

Avoid banners, narration, private issue links, change history, and comments that repeat the next statement. Document public behavior with [Google-style docstrings](docs/python-style.md#docstrings), not incidental implementation steps. A temporary workaround needs an explicit condition for removal.

## Security and conduct

Follow [SECURITY.md](SECURITY.md) for vulnerabilities. For ordinary bugs and feature requests, use the repository's issue templates and remove credentials, customer identifiers, audio, and transcripts from reproductions.

Keep discussion respectful, specific, and focused on the work. Explain tradeoffs, give actionable review feedback, and do not publish another person's private information. Maintainers may remove abusive or unsafe content.

## License

This project is licensed under [Apache-2.0](LICENSE). Contributions intentionally submitted for inclusion are provided under that license unless an existing written agreement states otherwise. Preserve applicable copyright and attribution notices, and disclose third-party material included with a contribution.
