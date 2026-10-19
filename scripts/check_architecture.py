"""Check dependency direction and the public initializer."""

from __future__ import annotations

import ast
from collections.abc import Sequence
import pathlib
import sys

# Every production module has an owner and an explicit first-party dependency
# set.
_DEPENDENCIES: dict[str, set[str]] = {
    "_defaults": set(),
    "errors": set(),
    "agent": set(),
    "config": {"_defaults"},
    "tracing": set(),
    "_http": {"_defaults", "errors"},
    "_connection": {"_defaults", "_http", "errors"},
    "_audio": {"_defaults", "agent", "errors"},
    "_session": {
        "_audio",
        "_connection",
        "_defaults",
        "_http",
        "agent",
        "errors",
        "tracing",
    },
    "_runs": {
        "_defaults",
        "_http",
        "_session",
        "agent",
        "config",
        "errors",
        "tracing",
    },
    "_cli": {
        "_defaults",
        "_http",
        "_runs",
        "_session",
        "agent",
        "config",
        "errors",
    },
    "client": {
        "_defaults",
        "_http",
        "_runs",
        "_session",
        "agent",
        "config",
        "tracing",
    },
    "__main__": {"_cli"},
    "__init__": {"agent", "client", "config", "errors", "tracing"},
}
_FACADES = {"__init__"}
_EXTERNAL_ROOTS = {"opentelemetry", "websockets"}


def check_package(package: pathlib.Path) -> list[str]:
    """Return violations of initializer, import, and module ownership rules.

    Args:
        package: Directory containing the production coval_voice modules.

    Returns:
        Diagnostics with module paths and one-based source lines.
    """
    violations: list[str] = []
    for path in sorted(package.rglob("*.py")):
        owner = path.relative_to(package).with_suffix("").as_posix()
        tree = ast.parse(path.read_text(), filename=str(path))

        if owner not in _DEPENDENCIES:
            violations.append(f"{path}: add the module's ownership policy")
            continue
        if owner == "__init__":
            _check_initializer(tree, path, violations)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for message in _check_import(node, owner, node in tree.body):
                violations.append(f"{path}:{node.lineno}: {message}")
    return violations


def _check_import(
    node: ast.Import | ast.ImportFrom, owner: str, top_level: bool
) -> list[str]:
    """Describe violations for one import without executing any module."""
    messages: list[str] = []
    if not top_level:
        messages.append("keep imports at module scope")
    if isinstance(node, ast.ImportFrom):
        if node.level:
            messages.append("use absolute imports")
        module = node.module or ""
        if module == "coval_voice":
            targets = [item.name for item in node.names]
        elif module.startswith("coval_voice."):
            targets = [module.removeprefix("coval_voice.")]
            if owner not in _FACADES:
                messages.append("import the owning module, not its symbols")
        else:
            targets = []
        roots = [module.split(".")[0]]
    else:
        roots = [item.name.split(".")[0] for item in node.names]
        targets = [
            item.name.removeprefix("coval_voice.")
            for item in node.names
            if item.name.startswith("coval_voice.")
        ]
        if any(item.name == "coval_voice" for item in node.names):
            messages.append("internal code must import an owning module")
    for target in targets:
        if target not in _DEPENDENCIES[owner]:
            messages.append(f"{owner} must not depend on {target}")
    for root in roots:
        if root not in (
            sys.stdlib_module_names | _EXTERNAL_ROOTS | {"coval_voice"}
        ):
            messages.append(f"undeclared production dependency: {root}")
    return messages


def _check_initializer(
    tree: ast.Module, path: pathlib.Path, violations: list[str]
) -> None:
    """Allow only a docstring, explicit exports, and a literal __all__."""
    exports: set[str] = set()
    declared: set[str] = set()
    for node in tree.body:
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue
        if isinstance(node, ast.ImportFrom):
            exports.update(item.asname or item.name for item in node.names)
            if all(item.name != "*" for item in node.names):
                continue
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "__all__"
        ):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                value = None
            if isinstance(value, list) and all(
                isinstance(x, str) for x in value
            ):
                declared.update(value)
                continue
        violations.append(
            f"{path}:{node.lineno}: initializer may only declare public exports"
        )
    if exports != declared:
        violations.append(
            f"{path}: imports must exactly match explicit __all__"
        )


def main(arguments: Sequence[str]) -> int:
    """Report violations and return a shell-compatible exit status.

    Args:
        arguments: Optional package directory; defaults to this checkout's SDK.

    Returns:
        Zero when the architecture policy passes, otherwise one.
    """
    package = (
        pathlib.Path(arguments[0])
        if arguments
        else pathlib.Path(__file__).resolve().parents[1] / "src" / "coval_voice"
    )
    if not package.is_dir():
        print(f"Missing package: {package}", file=sys.stderr)
        return 1
    violations = check_package(package)
    for violation in violations:
        print(violation, file=sys.stderr)
    return int(bool(violations))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
