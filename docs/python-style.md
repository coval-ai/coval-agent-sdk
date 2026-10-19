# Python Conventions

This repository follows the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html).
Ruff is our formatter and linter; strict Pyright checks production types. This is a
chosen tool substitution for Google's Pylint recommendation, not a claim that
Ruff implements every Google rule. Architectural intent and docstring accuracy
still require review. The checked-in tool versions and configuration are the
shared baseline for editors, local commands, and CI.

## Layout and Ownership

```text
src/
  coval_voice/
    __init__.py       # Explicit supported public API
    agent.py          # Callback protocol, streaming ABC, factory type
    config.py         # Immutable user configuration and validation
    errors.py         # API and relay failures
    client.py         # Public orchestration facade and legacy exports
    tracing.py        # Optional OpenTelemetry buffer and upload lifecycle
    _http.py          # Authenticated JSON requests; no launch retries
    _connection.py    # Offer discovery and one-use WebSocket upgrade
    _audio.py         # PCM pacing and bounded outgoing audio
    _session.py       # Agent/socket ownership and terminal cleanup
    _runs.py          # Launch, discovery, monitoring, sibling cancellation
    _cli.py           # Arguments, environment, adapter loading, exit status
    _defaults.py      # Internal protocol limits and timeouts
    py.typed
  coval_tunnel.py     # Legacy import/CLI shim
  coval_tracing.py    # Legacy trace import shim
scripts/              # Local and CI validation entry points
scripts/check_architecture.py
tests/               # Behavioral and installed-package contract tests
```

Tests live outside `src/`; no `sys.path` edits or pytest source-path injection.
Installation must work from the source distribution. Underscore modules are
internal. Their names and helper functions are not a supported extension API.

Split code when responsibilities, state ownership, or dependencies differ. Keep
code that changes for the same reason together. Review functions over roughly 40
lines and modules over roughly 400 lines for cohesive extractions; these are
review signals, not targets to satisfy with forwarding functions. A session's
cleanup must remain visible alongside its ownership rules. Ruff caps production function cyclomatic complexity at 15; review cohesive
extractions before adding more branches. Tests with scenario matrices are exempt
from that complexity budget. Do not introduce a
`utils.py`, `common.py`, a base class without a real shared contract, or a deep
package hierarchy to hide an unclear boundary. Move shared behavior to its owner
rather than copying it into another entry point.

Dependencies flow from the public client/CLI to run/session orchestration, then
connection/audio/HTTP primitives. Contracts, configuration, errors, and defaults
are leaf modules. Tracing does not import session orchestration. Internal modules
must not import the public facade or compatibility shims. The architecture check
checks this dependency graph, absolute first-party module imports, and the
package initializer's allowlist. Update that policy deliberately when a new
responsibility is introduced; do not silence it to accommodate a cycle.

## Imports and Initializers

Use absolute imports and module-qualified references:

```python
from coval_voice import config

settings = config.RunConfig(agent_id="a", persona_id="p", test_set_id="t")
```

Import modules, not their classes or functions. `typing`, `typing_extensions`, and
`collections.abc` symbol imports are allowed, matching Google's exceptions.
One import per line, with standard library, third-party, and first-party groups,
sorted lexicographically within each group. Ruff maintains the order. No wildcard,
relative, function-local, or optional imports to conceal a dependency cycle.
Use `from __future__ import annotations` in production modules with annotations.
Type-only imports must not hide an actual runtime dependency.

The root `__init__.py` is the deliberate exception: it reexports only the supported
user API and declares it in `__all__`. It contains no initialization calls, I/O,
configuration, registration, clients, logging setup, or wildcard exports. New
subpackages should have a docstring-only initializer unless they define an
explicit reviewed public facade. `client.py` retains explicit legacy reexports;
new internal code imports the owning module instead. The two top-level legacy
shims retain their existing alias behavior and are the only module-registry
aliases. Do not add new exports merely to make imports shorter.

## Formatting, Naming, and Types

Use four spaces, double quotes, trailing commas where the formatter inserts them,
and an 80-column target. Ruff's formatter may leave a literal longer than that;
split long diagnostic strings with adjacent literals while preserving the text.
Use `snake_case` for modules/functions/variables, `PascalCase` for classes, and
`UPPER_SNAKE_CASE` for constants. Avoid mutable global state and default arguments.
Use a leading underscore for implementation details.

Annotate production functions, including constructors, callbacks, and return
values. Use built-in generics and `X | None`, retaining Python 3.10 compatibility.
Prefer an explicit protocol or dataclass to an unstructured internal object.
`Any` is confined to JSON/network and dynamic CLI/plugin boundaries; validate
external shapes before depending on them. Do not spread `Any` or add casts merely
to conceal an incorrect contract. Cancellation must propagate after cleanup;
catch broad exceptions only at documented cleanup or exception-translation
boundaries. Keep buffers and deadlines bounded, and never retry mutations by
default.

## Docstrings

Use triple-double-quoted Google-style docstrings. Every production module, public
class, constructor, and public method/function needs a docstring. Describe the
observable contract, units, ownership, concurrency, cancellation, or failure
semantics where relevant. A short private helper may use a single sentence when
its signature and name fully explain it. Test names and assertions may supply the
behavior description without per-test docstrings.

Start with a concise sentence. Use a blank line before further explanation and
standard `Args:`, `Returns:`, `Yields:`, `Raises:`, and `Attributes:` sections as
applicable. Document each parameter's meaning; omit `self` and `cls`, and do not
repeat annotations in prose. Do not add an empty `Returns:` for `None`. Generators
use `Yields:`. Document errors callers should handle, not every incidental
exception a dependency could raise.

```python
async def connect(simulation_id: str) -> None:
    """Attach a fresh agent to an existing simulation.

    Args:
        simulation_id: Identifier of the hosted conversation to attach to.

    Raises:
        errors.TunnelError: Discovery or relay negotiation fails.
    """
```

Ruff checks Google section syntax, missing public docstrings, punctuation, and
argument documentation. It cannot establish that the text is correct or that all
meaningful return/error contracts are described; reviewers must check those.

## Enforcement and Exceptions

`scripts/lint` runs Ruff lint, Ruff format checks, architecture checks, and strict
Pyright. `scripts/check` adds wheel-from-sdist validation and installed-wheel
behavior tests. CI runs the complete check on Python 3.10, 3.12, and 3.14. It has
read-only repository permissions and no publication step.

The selected Ruff families cover formatting errors, imports, naming, modern
Python, bug patterns, annotations, docstrings, async pitfalls, security hazards,
and maintainability. We intentionally select stable rule families rather than
`ALL`, whose changing membership makes unrelated upgrades alter policy.
`ISC001` is disabled because it conflicts with the formatter. Test-only exceptions
are listed in `pyproject.toml` for assertions, synthetic credentials, unittest
assertions, unused fake parameters, annotations, docstrings, and scenario-matrix complexity. Production code
has no file-wide docstring or type-check exemptions. Use a narrow rule-specific
suppression with a concrete reason only when the contract requires it; unused
suppressions fail lint. Do not weaken assertions, skip cases, or introduce a
module-wide ignore to get checks green.
