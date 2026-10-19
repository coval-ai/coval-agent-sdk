# Release Preparation

This document covers maintainer preparation. `scripts/build` creates local artifacts; no command or workflow in this repository publishes a package. Repository visibility and package publication are separate, explicitly authorized actions.

## Before the first public release

- Settle the public API and CLI names, factory contract, error behavior, and pre-release version.
- Review the repository and Git history for credentials, customer material, private links, and copied material requiring attribution.
- Confirm ownership, `LICENSE`, `NOTICE`, SPDX metadata, and any third-party notices.
- Confirm a working private vulnerability-reporting channel and update `SECURITY.md` with its actual route.
- Confirm that README examples and installation instructions work for the intended release audience.
- Confirm the package-index project ownership and release credentials through the approved maintainer process.
- Review CI and branch protection settings; a workflow file alone does not make a check required for merging.

These are release criteria, not a claim that the repository is already public or a package version has been published.

## Build the candidate

1. Update `pyproject.toml` and `CHANGELOG.md` for the intended version; keep pre-release identifiers until the API is ready for a stable contract.
2. Run `scripts/check` on the supported CI Python versions when a release or compatibility change warrants that matrix.
3. Start with an empty `dist/` directory so old versions cannot be mistaken for the candidate, then run `scripts/build`.
4. Inspect the source archive and wheel for the package, `py.typed`, metadata, entry points, `LICENSE`, and `NOTICE`, with no tests or development artifacts shipped as importable packages.
5. Install the wheel into a clean consumer environment and exercise the relevant public API and CLI. Record any authorized hosted check separately from offline evidence.
6. Review the exact candidate artifacts and obtain authorization before tagging, publishing, or changing repository visibility.

Keep an unreleased change under `Unreleased` until a release actually occurs. Do not add a release date, registry badge, or `pip install` version claim based only on a successful local build. Once publication is authorized and implemented, document its actual workflow and permissions here.
