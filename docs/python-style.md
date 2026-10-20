# Python Conventions

This repository follows the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html). Ruff is the formatter and linter, and strict Pyright checks production types. Ruff replaces Google's recommended Pylint tool here; it does not implement every Google rule or replace review of architecture and docstring accuracy.

## Organization

Production code lives under `src/coval_agent_sdk/`, tests under `tests/`, and development commands under `scripts/`. [Architecture](architecture.md) defines each module's responsibility and dependency direction. Keep provider integrations outside the core package.

Split code when responsibility, state ownership, or dependencies differ. Keep code that changes for the same reason together. Review functions over roughly 40 lines and modules over roughly 400 lines for cohesive extractions; these are review signals, not targets to satisfy with forwarding functions. Keep a session's cleanup visible alongside its ownership rules.

Ruff caps production function cyclomatic complexity at 15. Review the owning operation before adding more branches. Scenario-matrix tests are exempt from that complexity budget. Do not introduce `utils.py`, `common.py`, a base class without a shared contract, or a deep package hierarchy to hide an unclear boundary.

## Imports

Use absolute imports and module-qualified references:

```python
from coval_agent_sdk import config

settings = config.RunConfig(agent_id="a", persona_id="p", test_set_id="t")
```

Import modules, not their classes or functions. Symbols from `typing`, `typing_extensions`, and `collections.abc` are allowed, matching Google's typing exceptions. The root `coval_agent_sdk/__init__.py` is the deliberate public-facade exception described below.

Use one import per line, with separate standard-library, third-party, and first-party groups sorted lexicographically within each group. Ruff maintains that order. Do not use wildcard or relative imports, or move an import inside a function to conceal a cycle. Use `from __future__ import annotations` in production modules with annotations; type-only imports must not hide a runtime dependency.

Internal modules import the owner of a definition, not the root facade. The architecture check enforces first-party module imports, top-level import placement, and dependency direction. Third-party module-import choices still need review.

## Initializers and public exports

`src/coval_agent_sdk/__init__.py` contains only its module docstring, explicit supported public imports, and a literal `__all__`. It performs no I/O, client construction, configuration, registration, or logging setup. The architecture check verifies that its imports and `__all__` agree.

A new subpackage starts with a docstring-only initializer unless it has a reviewed public facade. Keep launchers outside this library package. Do not expose internal helpers or add alternate import paths merely to shorten a reference. Update the public API test and documentation when an export intentionally changes.

## Formatting and names

Use four spaces, double quotes, and trailing commas where the formatter inserts them. Target 80 columns. Ruff may leave a long literal unsplit; split diagnostic strings into adjacent literals while preserving their text. Markdown follows the paragraph rule in [CONTRIBUTING.md](../CONTRIBUTING.md#where-prose-lives), not Python's line-length rule.

Use `snake_case` for modules, functions, variables, and parameters; `PascalCase` for classes; and `UPPER_SNAKE_CASE` for constants. Avoid mutable global state and mutable default arguments. A leading underscore marks an implementation detail, not a second supported API.

## Types and errors

Annotate production functions, including constructors, callbacks, and return values. Use built-in generics and `X | None` while retaining Python 3.10 compatibility. Prefer explicit protocols or dataclasses when an internal contract warrants one. Validate external JSON and dynamic adapter boundaries before depending on their shapes; do not spread `Any` or add casts to conceal an incorrect contract.

Keep buffers and deadlines bounded. Cancellation propagates after cleanup, and a cleanup failure must not replace the primary session error. Catch broad exceptions only at documented cleanup or exception-translation boundaries. Retry behavior belongs to the owner of the operation; never add a generic retry around a mutation.

## Docstrings

Use triple-double-quoted Google-style docstrings for production modules, public classes and their constructors, and public methods/functions. Describe the observable contract, units, ownership, concurrency, cancellation, and failure semantics where relevant. A short private helper may use one sentence when its signature and name explain it. Test names and assertions may supply the behavior description without a docstring on every test.

Start with a concise sentence and leave a blank line before additional explanation. Use standard `Args:`, `Returns:`, `Yields:`, `Raises:`, and `Attributes:` sections as applicable. Document parameter meanings without repeating annotations, and omit `self` and `cls`. Do not add an empty `Returns:` for `None`; generators use `Yields:`. Document errors callers should handle instead of every incidental dependency exception.

```python
async def connect(simulation_id: str) -> None:
    """Open a ready audio connection to an existing simulation.

    Args:
        simulation_id: Identifier of the hosted conversation to attach to.

    Raises:
        errors.CovalError: Discovery or relay negotiation fails.
    """
```

Ruff checks Google section syntax, public docstrings, punctuation, and argument documentation where sections are present. Reviewers must still verify that arguments, returns, errors, and lifecycle promises are accurate and complete.

## Enforcement and exceptions

`scripts/lint` runs Ruff lint, Ruff format checks, architecture checks, and strict Pyright. `scripts/check` adds wheel-from-source validation and installed-package behavior tests. CI uses the same complete check on Python 3.10, 3.12, and 3.14 with read-only repository permissions and no publication step.

The selected Ruff families cover formatting errors, imports, naming, modern Python, bug patterns, annotations, docstrings, async pitfalls, security hazards, and maintainability. Stable rule families are selected explicitly instead of using `ALL`, whose membership changes with unrelated upgrades. `ISC001` is disabled because it conflicts with the formatter.

Test-only exceptions in `pyproject.toml` allow assertions, synthetic credentials, unittest assertions, unused fake parameters, omitted annotations/docstrings, and scenario-matrix complexity. Production files have no blanket docstring or type-check exemption. Use a narrow rule-specific suppression with a concrete reason only when the contract requires it; unused suppressions fail lint. Do not weaken a test or introduce a module-wide ignore to make a check pass.
