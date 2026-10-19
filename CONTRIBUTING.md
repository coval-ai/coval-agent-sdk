# Contributing

Read [Python conventions](docs/python-style.md), [local development](docs/local-development.md),
and [developer API design](docs/developer-api.md) before changing the SDK.

Use Python 3.12 and `uv` for local development:

```sh
scripts/install
scripts/lint
scripts/check
```

`scripts/lint` checks Ruff, formatting, import boundaries, and strict production
Python types. `scripts/check` also builds and validates the installed wheel outside
the checkout and exercises the CLI. Neither command uses live credentials.
`./.venv/bin/ruff check --fix .` and `./.venv/bin/ruff format .` apply safe fixes;
review the diff before committing. Tests and development scripts follow the same
format and import conventions as production code. Test doubles may omit type
annotations and test methods may omit docstrings.

For a change, identify its owning module, preserve documented public imports, and
add behavior-focused coverage at affected boundaries. Keep provider integrations
in consumer repositories. A new public export needs API documentation and an
installed-package compatibility test. New dependencies need a concrete runtime
reason; a framework adapter is not a reason to add its framework to core.

Run the focused affected tests while iterating, then `scripts/check` before pushing.
CI uses these same entry points on supported Python versions. A green style check
does not replace review of lifecycle correctness, concurrency, or API design.
Keep PRs in draft while the API is being designed. Do not publish packages, change
repository visibility, or add release automation without an explicit request.
