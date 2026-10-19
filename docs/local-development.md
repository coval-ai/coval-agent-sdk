# Local Development

The SDK is developed in the private `coval-ai/coval-agent-sdk` repository. The initial `main` commit contains only the project README; the implementation and design notes are introduced through a draft PR. There is no package-registry publication or publishing workflow. Installation operates on a checked-out project directory or built wheel.

## Install and Edit

Run `scripts/install` from the project root. It creates a Python 3.12 virtual environment and installs the package with its development dependencies in editable mode. The runtime package supports Python 3.10 and newer; choosing 3.12 for local development does not change that compatibility contract. The dependency constraints in `pyproject.toml` are preserved from the extracted package.

Use `.venv/bin/python` and `.venv/bin/coval-tunnel` to select this environment explicitly. No API key is needed for installation, CLI help, tests, builds, or static type checking. Live runs require `COVAL_API_KEY`, a workspace selection, and existing synthetic agent/persona/test-set fixtures; a real agent adapter may also need provider credentials. Tests use controlled collaborators and do not launch simulations.

## Validate the Installed Package

Run `scripts/check`. It builds a source distribution and then a wheel from that source distribution, verifies the distribution metadata, installs the wheel into a temporary environment, and executes the three existing SDK test files and CLI help outside the project. It also checks the public package's Python types using `pyrightconfig.sdk.json`. The temporary environment is removed on exit. An explicit import check confirms Python loaded the isolated wheel installation rather than this source directory.

Run `scripts/build` when a consumer needs an artifact. It writes the wheel and source distribution to `dist/` and validates their metadata. Building a distribution does not publish it.

## Agent Playground Integration

The existing consumer is `agents/pipecat-sdk` in [agent-playground PR #93](https://github.com/coval-ai/agent-playground/pull/93). Its `COVAL_VOICE_SDK_WHEEL` override can install a locally built wheel without a registry. The current distribution name and version are retained so this mechanism remains compatible. The PR's default install still resolves a package-index version; it has not been rewritten as part of extraction.

Once the developer API settles, update the consumer dependency and verify its deterministic pipeline tests against the standalone wheel. Repeat the bounded hosted canary when runtime contracts change, recording source and wheel identities, saved audio/transcript, metric outcomes, cleanup, and optional trace upload. The extracted implementation has already been exercised through a standalone wheel; that evidence does not verify future API changes. Live acceptance and outstanding provider, relay, and evaluation limits are tracked in COVAL-7087.

The Pipecat agent remains in agent-playground. The simulation worker, API routes, relay, infrastructure, and application deployments remain in their owning projects. No backend or playground source files are removed by creating this local project.
