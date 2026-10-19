"""Exercise the architecture command's accept and reject boundaries."""

import pathlib
import subprocess
import sys

import pytest

_CHECKER = (
    pathlib.Path(__file__).resolve().parents[1]
    / "scripts"
    / "check_architecture.py"
)


def _check(tmp_path, module, source):
    package = tmp_path / "coval_agent_sdk"
    package.mkdir()
    (package / f"{module}.py").write_text(source)
    return subprocess.run(  # noqa: S603 - Fixed interpreter and local checker.
        [sys.executable, str(_CHECKER), str(package)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("module", "source"),
    [
        ("_session", "from coval_agent_sdk import _connection\n"),
        ("config", "from coval_agent_sdk import _defaults\n"),
        ("__main__", "from coval_agent_sdk import _cli\n"),
        (
            "__init__",
            (
                "from coval_agent_sdk.client import CovalVoiceClient\n"
                '__all__ = ["CovalVoiceClient"]\n'
            ),
        ),
    ],
)
def test_architecture_accepts_owning_modules_and_explicit_exports(
    tmp_path, module, source
):
    result = _check(tmp_path, module, source)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("module", "source", "diagnostic"),
    [
        ("_session", "from coval_agent_sdk import client\n", "must not depend"),
        (
            "client",
            "from coval_agent_sdk._session import run_session\n",
            "owning module",
        ),
        (
            "_session",
            "from coval_agent_sdk._connection import wait_for_offer\n",
            "owning module",
        ),
        ("_session", "from . import _connection\n", "absolute imports"),
        ("_http", "import django\n", "undeclared production dependency"),
        ("misc", "", "ownership policy"),
        ("__init__", "initialize()\n", "only declare public exports"),
        (
            "__init__",
            "from coval_agent_sdk.client import CovalVoiceClient\n",
            "exactly match",
        ),
        ("__init__", "__all__ = make_exports()\n", "only declare"),
        (
            "_session",
            "def hidden_import():\n    import asyncio\n",
            "module scope",
        ),
    ],
)
def test_architecture_rejects_hidden_dependencies_and_initializer_work(
    tmp_path, module, source, diagnostic
):
    result = _check(tmp_path, module, source)
    assert result.returncode == 1
    assert diagnostic in result.stderr
