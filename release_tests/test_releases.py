"""Exercise real release tooling in isolated Git repositories."""

import os
import pathlib
import subprocess
import sys
import tomllib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TOOL = _ROOT / ".venv" / "bin" / "semantic-release"
_VALIDATOR = _ROOT / "scripts" / "validate_release.py"


def _environment(**values):
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in {"PATH", "HOME", "TMPDIR"}
    }
    return {
        **environment,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        **values,
    }


def _run(repository, *command, check=True, **environment):
    return subprocess.run(  # noqa: S603 -- Test-owned command and fixtures.
        command,
        cwd=repository,
        env=_environment(**environment),
        capture_output=True,
        text=True,
        check=check,
    )


def _git(repository, *arguments):
    return _run(repository, "git", *arguments).stdout.strip()


def _commit(repository, message):
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", message)


@pytest.fixture
def repository(tmp_path):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "-b", "main")
    _git(checkout, "config", "user.name", "Synthetic release test")
    _git(checkout, "config", "user.email", "test@example.test")
    _git(checkout, "config", "commit.gpgsign", "false")
    (checkout / "pyproject.toml").write_text(
        (_ROOT / "pyproject.toml")
        .read_text()
        .replace(
            'url = "https://github.com/coval-ai/coval-agent-sdk.git"',
            'url = "https://github.com/example/sdk-release-fixture.git"',
        )
    )
    (checkout / "CHANGELOG.md").write_text("# Changelog\n")
    _commit(checkout, "feat: connect a synthetic voice agent")
    _git(checkout, "remote", "add", "origin", str(remote))
    _git(checkout, "push", "-u", "origin", "main")
    return checkout


def _prepare(repository):
    output = repository.parent / "release-output"
    output.write_text("")
    _run(
        repository,
        str(_TOOL),
        "version",
        "--no-push",
        "--no-vcs-release",
        "--skip-build",
        GITHUB_OUTPUT=str(output),
    )
    return output.read_text()


def _validate(repository, tag, mode="prepared", actor=""):
    return _run(
        repository,
        sys.executable,
        str(_VALIDATOR),
        mode,
        tag,
        check=False,
        GITHUB_ACTOR=actor,
    )


def _push(repository, tag):
    _git(repository, "push", "--atomic", "origin", "HEAD:main", tag)


def test_real_tool_generates_initial_patch_minor_and_breaking_releases(
    repository,
):
    cases = [
        (None, "0.1.0", "Connect a synthetic voice agent"),
        ("fix: retain caller audio", "0.1.1", "Retain caller audio"),
        ("perf: pace output efficiently", "0.1.2", "Pace output efficiently"),
        ("feat: add tracing", "0.2.0", "Add tracing"),
        (
            "feat!: replace the audio contract",
            "1.0.0",
            "Replace the audio contract",
        ),
        (
            (
                "fix: require explicit audio settings\n\n"
                "BREAKING CHANGE: callers must supply settings"
            ),
            "2.0.0",
            "Require explicit audio settings",
        ),
    ]
    for index, (message, version, note) in enumerate(cases):
        if message:
            (repository / "synthetic.txt").write_text(str(index))
            _commit(repository, message)
            _git(repository, "push", "origin", "main")
        output = _prepare(repository)
        tag = f"v{version}"
        assert "released=true\n" in output
        assert f"tag={tag}\n" in output
        assert _git(repository, "log", "-1", "--format=%s") == (
            f"chore(release): {version}"
        )
        assert _git(repository, "tag", "--points-at", "HEAD") == tag
        metadata = tomllib.loads((repository / "pyproject.toml").read_text())
        assert metadata["project"]["version"] == version
        changelog = (repository / "CHANGELOG.md").read_text()
        assert f"## {tag} (" in changelog
        assert note in changelog
        result = _validate(repository, tag)
        assert result.returncode == 0, result.stderr
        _push(repository, tag)
        result = _validate(
            repository, tag, "tag", "coval-release-automation[bot]"
        )
        assert result.returncode == 0, result.stderr

    head = _git(repository, "rev-parse", "HEAD")
    (repository / "maintenance.txt").write_text("Synthetic maintenance.\n")
    _commit(repository, "docs: clarify the example")
    _git(repository, "push", "origin", "main")
    maintenance_head = _git(repository, "rev-parse", "HEAD")
    assert head != maintenance_head
    assert "released=false\n" in _prepare(repository)
    assert _git(repository, "rev-parse", "HEAD") == maintenance_head
    assert (repository / "CHANGELOG.md").read_text() == changelog
    assert "released=false\n" in _prepare(repository)
    assert _git(repository, "rev-parse", "HEAD") == maintenance_head


@pytest.fixture
def prepared(repository):
    _prepare(repository)
    control = _validate(repository, "v0.1.0")
    assert control.returncode == 0, control.stderr
    return repository


@pytest.mark.parametrize("tag", ["v0.2.0", "v01.1.0", "v0.1.0a1"])
def test_rejects_mismatched_or_non_final_tags(prepared, tag):
    assert _validate(prepared, tag).returncode != 0


def test_rejects_tag_outside_main_and_wrong_actor(prepared):
    result = _validate(prepared, "v0.1.0", "tag", "jakelevi")
    assert "Release tag must be pushed by the release App" in result.stderr
    result = _validate(
        prepared, "v0.1.0", "tag", "coval-release-automation[bot]"
    )
    assert result.returncode != 0
    _push(prepared, "v0.1.0")
    assert (
        _validate(
            prepared, "v0.1.0", "tag", "coval-release-automation[bot]"
        ).returncode
        == 0
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("source", "Release commit may only update version and changelog"),
        ("metadata", "Release metadata may only change project.version"),
        ("changelog", "Release requires a generated changelog entry"),
    ],
)
def test_rejects_release_commits_that_change_the_contract(
    prepared, change, message
):
    if change == "source":
        (prepared / "agent.py").write_text("raise RuntimeError\n")
    elif change == "metadata":
        metadata = prepared / "pyproject.toml"
        metadata.write_text(
            metadata.read_text().replace(
                'requires-python = ">=3.10"', 'requires-python = ">=3.14"'
            )
        )
    else:
        (prepared / "CHANGELOG.md").write_text(
            "# Changelog\n\nSynthetic missing release entry.\n"
        )
    _git(prepared, "add", ".")
    _git(prepared, "commit", "--amend", "--no-edit")
    result = _validate(prepared, "v0.1.0")
    assert message in result.stderr


def test_rejects_stale_preparation_and_dirty_checkouts(prepared):
    (prepared / "scratch.txt").write_text("Synthetic uncommitted change.\n")
    result = _validate(prepared, "v0.1.0")
    assert "Release requires a clean checkout" in result.stderr
    (prepared / "scratch.txt").unlink()
    _git(prepared, "update-ref", "refs/remotes/origin/main", "HEAD")
    result = _validate(prepared, "v0.1.0")
    assert "Release must be prepared from current main" in result.stderr


def test_atomic_push_keeps_unpublished_tag_local_when_main_diverges(prepared):
    concurrent = prepared.parent / "concurrent"
    _git(
        prepared.parent,
        "clone",
        "-b",
        "main",
        str(prepared.parent / "remote.git"),
        str(concurrent),
    )
    _git(concurrent, "config", "user.name", "Synthetic concurrent author")
    _git(concurrent, "config", "user.email", "other@example.test")
    _git(concurrent, "config", "commit.gpgsign", "false")
    (concurrent / "newer.txt").write_text("Synthetic concurrent work.\n")
    _commit(concurrent, "docs: add a concurrent example")
    _git(concurrent, "push", "origin", "main")
    concurrent_head = _git(concurrent, "rev-parse", "HEAD")
    result = _run(
        prepared,
        "git",
        "push",
        "--atomic",
        "origin",
        "HEAD:main",
        "v0.1.0",
        check=False,
    )
    assert result.returncode != 0
    assert _git(prepared, "ls-remote", "--tags", "origin") == ""
    assert (
        _git(prepared, "ls-remote", "--heads", "origin", "main").split()[0]
        == concurrent_head
    )


@pytest.mark.parametrize("tampered", [False, True])
def test_real_release_candidate_requires_independent_validation(
    prepared, tampered
):
    base = _git(prepared, "rev-parse", "HEAD^")
    if tampered:
        path = prepared / "pyproject.toml"
        path.write_text(
            path.read_text() + '\n[tool.unexpected]\ncode = "injected"\n'
        )
        _git(prepared, "add", ".")
        _git(prepared, "commit", "--amend", "--no-edit")
    artifact = prepared.parent / "candidate-data"
    helper = _ROOT / "scripts/release_candidate.py"
    _run(
        prepared,
        sys.executable,
        str(helper),
        "export",
        "release",
        str(artifact),
        "--tag",
        "v0.1.0",
    )
    publisher = prepared.parent / "publisher"
    _git(
        prepared.parent,
        "clone",
        "--branch",
        "main",
        str(prepared.parent / "remote.git"),
        str(publisher),
    )
    _run(
        publisher,
        sys.executable,
        str(helper),
        "prepare",
        "release",
        str(artifact),
        "--tag",
        "v0.1.0",
        "--base",
        base,
    )
    result = _validate(publisher, "v0.1.0")
    if tampered:
        assert result.returncode != 0
        assert "may only change project.version" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        _git(
            publisher,
            "push",
            "--atomic",
            "origin",
            "HEAD:refs/heads/main",
            "refs/tags/v0.1.0",
        )
    remote = _git(publisher, "ls-remote", "origin", "refs/heads/main")
    assert remote.startswith(base) is tampered
