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

Use squash merges with a concise Conventional Commit PR title such as `fix: preserve cancellation during trace cleanup` or `docs: clarify audio connection ownership`. Commit and PR bodies explain why the change is needed; they should not narrate every command run.

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

Release tooling uses Python 3.12 and the pinned `python-semantic-release` development dependency. After changing its configuration, workflow, or validator, also run `.venv/bin/python -m pytest release_tests -q`. This separate suite exercises real release tooling in isolated Git repositories without contacting GitHub or PyPI; CI runs it in a dedicated job. It is not part of the installed SDK runtime suite.

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

The public wire tests use loopback HTTP and TLS WebSocket servers. They require `openssl` on `PATH` to generate a temporary certificate and key; no test credentials are committed. Only the local CA trust and polling interval are injected into the client. Discovery, the HTTP adapter, WebSocket upgrade, audio exchange, monitoring, and public result construction execute normally. These tests prove the local protocol path, not deployed Cloudflare or production behavior.

## API and dependencies

Add public exports deliberately and document their ownership, inputs, outputs, error behavior, and cleanup obligations. Keep the importable package independent of any particular agent framework. Put unimplemented interfaces in [API design](docs/developer-api.md), clearly marked as proposals.

Expose conversation audio connections without agent classes or factories. Applications own agent/pipeline construction and cleanup; SDK connection contexts and async run handlers own only audio transport and simulation orchestration. Framework plugins belong in consumers, and application cleanup must finish before trace finalization.

Keep the SDK a Python library only. Return structured results and raise failures without printing, emitting log records, configuring logging, or adding console-script or package-execution entry points. Calling applications own presentation. Preserve useful diagnostics in returned outcomes and errors as described in [runtime contracts](docs/runtime.md#returned-outcomes); test that affected public operations return the expected information without writing console output or log records.

Keep `coval-sdk` and its HTTP dependency inside the private `_http.py` adapter. Do not expose dependency clients, configuration, models, raw responses, or exception types through public attributes, parameters, return values, or exports. Translate request failures into this package's errors at that boundary; orchestration owns read retries, and run creation must remain a single attempt. Do not enable dependency debug output or change application logging configuration.

Derive SDK-owned failures from `CovalError` and retain useful specific subclasses. Do not wrap application exceptions or cancellation as SDK errors. Preserve the original failure through session cleanup and batch sibling cancellation; attach `RunError` snapshots only to SDK failures after launch. Completed-session trace delivery failures remain structured diagnostics.

Put optional integration dependencies in a named extra and declare every external package imported directly by that integration. Keep the base SDK importable and runnable without those dependencies. OpenTelemetry belongs only in `coval_agent_sdk.tracing`; use its protobuf encoder with the private HTTP adapter rather than an exporter that emits logs or hides retry policy. Core code uses the dependency-free `TraceExporter` contract. `scripts/check` first exercises a clean base wheel installation, then installs `[tracing]` and runs the full suite.

For a new dependency, explain why the SDK needs it, its Python support, and its distribution/license implications. Update the affected tests and package metadata. Review dependency upgrades as code changes; do not relax constraints solely to silence a failing check.

Release automation owns the version in `pyproject.toml` and [CHANGELOG.md](CHANGELOG.md). Do not bump versions or edit the changelog in feature PRs. Squash-merged Conventional Commit titles supply the release notes: `fix:` and `perf:` produce a patch, `feat:` a minor, and `!` or `BREAKING CHANGE:` a major, including a move from `0.x` to `1.0.0`. Other types do not trigger a release on their own. Follow [releasing](docs/releasing.md) for setup, retries, and publication evidence.

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

## Documentation and comments

Document the current behavior and the decisions a developer needs to use or change it. The ownership map above determines where an explanation belongs; keep a single authoritative explanation and link to it from other pages.

### Write a comment when

- A value, limit, dependency pin, or protocol choice has a reason the code cannot express.
- Changing the behavior has a consequence outside the function, such as a wire-contract change, resource leak, cancellation interaction, or broken consumer.
- Correctness depends on ordering, concurrency, or another component's behavior.
- A temporary workaround has a specific condition for removal.

### Leave a comment out when

- It repeats a name, annotation, expression, or the next statement.
- It is a banner, divider, file-level narrative, or an explanation repeated in sibling modules.
- It recounts a previous implementation, rejected approach, ticket, pull request, or incident; durable delivery history belongs in the work tracker.
- It makes a claim that will drift, such as an unexplained current count or "for now" without an exit condition.

### Form and review

Place a comment immediately above the code it explains. Use concise plain sentences; put a longer explanation in a README or docs page and link to that path. Prefer documentation links over private issue or pull-request links. Rule-specific tool suppressions may remain inline when the checker requires it; include their concrete reason.

Delete the comment mentally and read the code again. If no useful information disappears, remove it. If information does disappear, first check whether a clearer name, type, or structure can express it directly.

Use one paragraph per line in Markdown and structured lists/tables where useful. Python comments and docstrings follow the Google-based 80-column conventions in [Python conventions](docs/python-style.md); the Markdown wrapping rule does not override the formatter.

### Keep documentation current

When changing an API, configuration value, dependency, behavior, or command, search README files, docs, contribution guidance, templates, and examples for its name and update every affected claim in the same change. Recheck "all", "every", and "only" claims against the resulting implementation.

Describe supported behavior separately from proposals and release prerequisites. Verify local links and anchors, check examples and commands against the actual API, and distinguish syntax checks, offline tests, and hosted evidence. Do not imply publication, deployment, or live acceptance from a successful local build.

### Docstrings

Document public behavior with [Google-style docstrings](docs/python-style.md#docstrings), including meaningful arguments, results, errors, units, ownership, and cancellation where relevant. Docstrings explain the callable's contract; code comments explain constraints at the point they matter.

## Security and conduct

Follow [SECURITY.md](SECURITY.md) for vulnerabilities. For ordinary bugs and feature requests, use the repository's issue templates and remove credentials, customer identifiers, audio, and transcripts from reproductions.

Keep discussion respectful, specific, and focused on the work. Explain tradeoffs, give actionable review feedback, and do not publish another person's private information. Maintainers may remove abusive or unsafe content.

## License

This project is licensed under [Apache-2.0](LICENSE). Contributions intentionally submitted for inclusion are provided under that license unless an existing written agreement states otherwise. Preserve applicable copyright and attribution notices, and disclose third-party material included with a contribution.
