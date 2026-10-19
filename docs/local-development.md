# Local Development

Use Python 3.12 and `uv` for the normal development environment. Runtime code supports Python 3.10 and newer; local and CI checks must preserve that compatibility.

## Editable installation

```sh
scripts/install
.venv/bin/python -c "import coval_agent_sdk"
```

`scripts/install` creates `.venv` when absent and installs the SDK with development and tracing dependencies in editable mode. Source changes are immediately available in that environment. To choose another supported interpreter in a fresh checkout, set `PYTHON_VERSION` when running the install script.

Use `.venv/bin/python` explicitly. No API key is needed for setup, imports, lint, type checking, builds, or tests. Live use requires an authorized Coval API key, workspace, and synthetic agent/persona/test-set fixtures; an adapter may also need its own provider credentials.

## Checks and builds

```sh
scripts/lint
scripts/check
scripts/build
```

`scripts/lint` checks Ruff lint/format, the import/dependency policy, and strict production types. `scripts/check` adds a source-distribution build, a wheel built from that source distribution, metadata checks, a dependency-free base-install probe, and execution of the full tracing-enabled tests against the wheel in a temporary environment outside the checkout. It verifies imports resolve to that installation rather than the source directory. The temporary environment is removed on exit.

`scripts/build` writes a wheel and source archive to `dist/` and validates their metadata. A build does not upload either artifact. The Ruff and Pyright versions are pinned in `pyproject.toml`; runtime dependency constraints are intentionally separate from development tool selection.

## Use the SDK in another project

For fast local iteration, install this checkout into the consumer's environment:

```sh
uv pip install --python /path/to/consumer/.venv/bin/python \
  -e /path/to/coval-agent-sdk
```

For packaging acceptance, build and install the wheel instead:

```sh
scripts/build
uv pip install --python /path/to/consumer/.venv/bin/python \
  dist/coval_agent_sdk-0.2.0a1-py3-none-any.whl
```

Run the consumer's relevant tests against that installed wheel. Editable-install success does not prove that the source archive or wheel contains every required file.

The Agent Playground Pipecat adapter is one consumer. Its local install hook accepts `COVAL_AGENT_SDK_WHEEL=/absolute/path/to/the/wheel`. Provider configuration and framework dependencies stay with that adapter, not in this package.

## Live checks

Choose the target API root and workspace explicitly and use synthetic fixtures. Record the source/artifact identity, run and simulation identifiers, terminal outcome, audio/transcript evidence, cleanup, and optional trace upload. Distinguish a completed voice session from a completed batch and from quality-metric results.

Do not put credentials or collected conversation data into the repository. A hosted check needs existing authorization for the selected environment; offline checks remain the default development loop.
