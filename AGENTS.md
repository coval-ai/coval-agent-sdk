# AGENTS.md

Instructions for coding agents working on the Coval Agent SDK. The closest applicable `AGENTS.md` governs its directory; explicit user instructions take precedence. Follow existing authorization without asking for it again.

## Before you start

- Read `README.md` for the supported installation and developer API.
- Read `CONTRIBUTING.md` before changing code or preparing a pull request.
- Read `AGENTS.meta.md` before editing either agent-guidance file.
- Read `docs/python-style.md` before adding modules, imports, exports, types, docstrings, or lint exceptions.
- Read `docs/architecture.md` before changing dependency direction or moving state between modules.
- Read `docs/runtime.md` before changing audio, retries, deadlines, cancellation, or trace cleanup.
- Read `docs/developer-api.md` before turning a proposed interface into executable behavior.

## Commands

- Run `scripts/install` from the repository root to create the development environment and install the editable SDK.
- Run `.venv/bin/ruff check --fix .` and `.venv/bin/ruff format .` to apply safe lint fixes and formatting, then review the diff.
- Run `scripts/lint` after Python changes; it checks Ruff, formatting, architecture, and strict production types.
- Run `scripts/check` after runtime, entry-point, dependency, or packaging changes; it also tests a wheel built from the source distribution outside the checkout.
- Run `scripts/build` when a consumer needs a local wheel; validate the artifact without publishing it.
- Check local links and run `git diff --check` for documentation-only changes; run broader checks only when the affected behavior warrants them.
- Use the same commands locally and in `.github/workflows/checks.yml` instead of adding a separate CI-only validation path.

## Python

- Follow the Google-based conventions in `docs/python-style.md`; use four spaces, double quotes, an 80-column target, and the pinned Ruff configuration.
- Import absolute owning modules inside the SDK; keep symbol imports limited to typing helpers and the explicit root API facade.
- Keep `src/coval_agent_sdk/__init__.py` limited to its docstring, explicit public imports, and literal `__all__`; `scripts/check_architecture.py` checks this boundary.
- Annotate production parameters and return values, and keep `Any` at validated JSON or dynamic adapter boundaries.
- Write Google-style docstrings for public APIs, including meaningful `Args:`, `Returns:`, `Yields:`, `Raises:`, and `Attributes:` sections where applicable.
- Split code by responsibility and state ownership rather than creating generic utility modules or forwarding layers to satisfy a size target.
- Use a rule-specific suppression with its concrete reason instead of weakening a production file's lint or type policy.

## Package and API

- Use `coval-agent-sdk` as the distribution name, `coval_agent_sdk` as the Python package, and the library API documented in `README.md`.
- Treat this unreleased SDK as a clean slate; update consumers alongside API changes without compatibility aliases or deprecation paths.
- Return outcomes and raise failures without printing, logging, configuring logging, or adding CLI entry points.
- Keep internal orchestration out of public client reexports; expose a definition through the root facade only when it is part of the reviewed developer API.
- Keep provider frameworks, backend imports, AWS configuration, and consumer source paths outside the core SDK.
- Keep optional integration dependencies behind extras and outside core imports; follow the dependency policy in `CONTRIBUTING.md`.
- Add a dependency only for a concrete runtime or development requirement and keep its supported Python range consistent with `pyproject.toml`.
- Update `README.md`, public docstrings, `CHANGELOG.md`, and affected examples when a supported interface changes.
- Include `LICENSE` and `NOTICE` in source and wheel distributions, and keep the SPDX expression in `pyproject.toml` consistent with them.

## Runtime

- Expose audio connections without SDK agent classes or factories; keep application pipeline construction and lifecycle outside the SDK.
- Bound unread input, await serialized output writes, and preserve PCM16 validation and cross-call pacing.
- Retry only documented transient reads and unconsumed relay upgrades; do not automatically repeat run creation or a consumed connection attempt.
- Preserve the primary session error through cancellation, application cleanup, and trace cleanup.
- Accept a missing terminal frame only when the simulation API explicitly confirms completion.
- Upload completed conversations separately and discard failed-session traces after closure and flush.
- Use synthetic fixtures and deterministic local collaborators for automated tests; live simulations need an explicitly selected environment and existing authorization.
- Never commit credentials, one-use connection tokens, customer audio, or transcripts; secret scanning cannot establish that every fixture is safe.

## Tests

- Follow `CONTRIBUTING.md` for behavior-focused tests and reviewable changes.
- Keep tests outside `src/` and exercise installed-package boundaries without modifying `sys.path`.
- Patch dependencies at the module that owns them and keep the behavior under test real.
- Preserve assertions for failure, cancellation, cleanup, and supported public entry points when reorganizing tests.
- Distinguish local, mocked, and hosted evidence; do not infer a completed simulation from a successful launch or connection.

## Documentation and comments

- Read `CONTRIBUTING.md`'s "Documentation and comments" section before writing documentation or code comments.
- Follow its document ownership, comment purpose, placement, and review rules; keep explanations in one authoritative page and link from other pages.
- Search README files, docs, guidance, templates, and examples for changed APIs or behavior, and update affected claims in the same change.
- Write Markdown paragraphs on one line; keep Python comments and docstrings within the Google-based formatting conventions.
- Keep public documentation focused on current supported behavior; retain internal delivery history in the work tracker and label proposals and unverified evidence explicitly.

## Releases and security

- Follow `docs/releasing.md` when preparing a version or distribution.
- Obtain explicit authorization before publishing packages, changing repository visibility, or adding publishing automation.
- Preserve an existing pull request's draft state unless the user authorizes changing it.
- Follow `SECURITY.md` for vulnerability reports and avoid putting sensitive reproduction details in public issues.
