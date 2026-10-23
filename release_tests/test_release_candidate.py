"""Exercise artifact promotion against isolated local Git repositories."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/release_candidate.py"
VERSION = "pyproject.toml"
TAG = "v1.2.3"


def run(root, *command, check=True):
    result = subprocess.run(  # noqa: S603 -- Test-owned Git/Python arguments.
        command,
        cwd=root,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull},
    )
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


def git(root, *arguments):
    return run(root, "git", *arguments).stdout.strip()


@pytest.fixture
def candidate(tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    builder = tmp_path / "builder"
    git(tmp_path, "clone", str(remote), str(builder))
    git(builder, "config", "user.name", "Synthetic maintainer")
    git(builder, "config", "user.email", "maintainer@example.test")
    git(builder, "config", "commit.gpgsign", "false")
    path = builder / VERSION
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[project]\nversion = "1.2.2"\n')
    (builder / "CHANGELOG.md").write_text("# Changelog\n")
    (builder / "README.md").write_text("untouched\n")
    git(builder, "add", ".")
    git(builder, "commit", "-m", "initial")
    git(builder, "push", "origin", "main")
    base = git(builder, "rev-parse", "HEAD")
    publisher = tmp_path / "publisher"
    git(tmp_path, "clone", str(remote), str(publisher))
    path.write_text('[project]\nversion = "1.2.3"\n')
    (builder / "CHANGELOG.md").write_text(
        "# Changelog\n\n## v1.2.3\n\nSynthetic fix.\n"
    )
    git(builder, "add", ".")
    git(builder, "commit", "-m", "candidate")
    artifact = tmp_path / "artifact"
    run(
        builder,
        sys.executable,
        str(SCRIPT),
        "export",
        "release",
        str(artifact),
        "--tag",
        TAG,
    )
    return builder, publisher, artifact, base


def prepare(candidate, **overrides):
    _, publisher, artifact, base = candidate
    return run(
        publisher,
        sys.executable,
        str(SCRIPT),
        "prepare",
        "release",
        str(artifact),
        "--tag",
        overrides.get("tag", TAG),
        "--base",
        overrides.get("base", base),
        check=False,
    )


def replace_artifact(candidate):
    builder, _, artifact, base = candidate
    patch = subprocess.check_output(  # noqa: S603 -- Fixed Git diff arguments.
        [  # noqa: S607 -- Git is a required local test tool.
            "git",
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-renames",
            base,
            "HEAD",
        ],
        cwd=builder,
    )
    (artifact / "candidate.patch").write_bytes(patch)
    manifest_path = artifact / "candidate.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["patch_sha256"] = hashlib.sha256(patch).hexdigest()
    manifest_path.write_text(json.dumps(manifest))


def test_candidate_is_data_until_independently_promoted(candidate):
    _, publisher, _, base = candidate
    assert git(publisher, "ls-remote", "origin", "refs/heads/main").startswith(
        base
    )
    result = prepare(candidate)
    assert result.returncode == 0, result.stderr
    assert git(publisher, "rev-parse", "HEAD^") == base
    assert git(publisher, "rev-parse", TAG + "^{commit}") == git(
        publisher, "rev-parse", "HEAD"
    )
    assert (
        git(publisher, "show", "-s", "--format=%s", "HEAD")
        == "chore(release): 1.2.3"
    )
    assert (publisher / "README.md").read_text() == "untouched\n"
    assert git(publisher, "status", "--porcelain") == ""
    assert git(publisher, "ls-remote", "origin", "refs/heads/main").startswith(
        base
    )
    git(
        publisher,
        "push",
        "--atomic",
        "origin",
        "HEAD:refs/heads/main",
        "refs/tags/" + TAG,
    )
    assert not git(
        publisher, "ls-remote", "origin", "refs/heads/main"
    ).startswith(base)


@pytest.mark.parametrize(
    "field", ["patch_sha256", "base", "kind", "tag", "source"]
)
def test_manifest_mismatch_cannot_prepare_or_move_main(candidate, field):
    _, publisher, artifact, base = candidate
    path = artifact / "candidate.json"
    manifest = json.loads(path.read_text())
    manifest[field] = "tampered"
    path.write_text(json.dumps(manifest))
    result = prepare(candidate)
    assert result.returncode != 0
    assert "integrity or source identity" in result.stderr
    assert git(publisher, "rev-parse", "HEAD") == base


def test_corrupted_patch_is_rejected(candidate):
    _, publisher, artifact, base = candidate
    with (artifact / "candidate.patch").open("ab") as stream:
        stream.write(b"tampered")
    assert prepare(candidate).returncode != 0
    assert git(publisher, "rev-parse", "HEAD") == base


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        ".github/workflows/attack.yml",
        "scripts/validate_release.py",
    ],
)
def test_disallowed_paths_are_never_materialized(candidate, path):
    builder, publisher, _, base = candidate
    changed = builder / path
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.write_text("unauthorized\n")
    git(builder, "add", ".")
    git(builder, "commit", "-m", "unauthorized extra file")
    replace_artifact(candidate)
    result = prepare(candidate)
    assert result.returncode != 0
    assert "unauthorized path" in result.stderr
    assert git(publisher, "rev-parse", "HEAD") == base
    assert (
        not (publisher / path).exists()
        or (publisher / path).read_text() == "untouched\n"
    )


def test_symlink_output_cannot_be_promoted(candidate, tmp_path):
    builder, publisher, _, base = candidate
    target = tmp_path / "victim"
    target.write_text("private control\n")
    path = builder / VERSION
    path.unlink()
    path.symlink_to(target)
    git(builder, "add", VERSION)
    git(builder, "commit", "-m", "symlink attack")
    replace_artifact(candidate)
    result = prepare(candidate)
    assert result.returncode != 0
    assert "regular files" in result.stderr
    assert git(publisher, "rev-parse", "HEAD") == base
    assert target.read_text() == "private control\n"


def test_current_main_must_still_match_the_workflow_revision(candidate):
    builder, publisher, _, base = candidate
    git(builder, "checkout", "main")
    git(builder, "push", "origin", "HEAD:refs/heads/main")
    newer = git(builder, "rev-parse", "HEAD")
    assert newer != base
    result = prepare(candidate)
    assert result.returncode != 0
    assert "Main changed" in result.stderr
    assert git(publisher, "rev-parse", "HEAD") == base
    assert git(publisher, "ls-remote", "origin", "refs/heads/main").startswith(
        newer
    )


def test_release_workflow_separates_credentials_from_candidate_execution():
    config = yaml.safe_load(
        (ROOT / ".github/workflows/release-automation.yml").read_text(),
    )
    assert config["permissions"] == {"contents": "read"}
    producer = config["jobs"]["release"]
    assert "secrets." not in json.dumps(producer)
    promoter = config["jobs"]["promote"]
    assert promoter["environment"] == "release-automation"
    assert "github.ref == 'refs/heads/main'" in promoter["if"]
    steps = promoter["steps"]
    token = next(
        i for i, step in enumerate(steps) if step.get("id") == "app-token"
    )
    validator = next(
        i
        for i, step in enumerate(steps)
        if "validate_release.py prepared" in step.get("run", "")
    )
    assert validator < token
    assert "scripts/install" not in json.dumps(promoter)
    assert ".venv/" not in json.dumps(promoter)
    for step in steps:
        if "uses" in step:
            assert len(step["uses"].split("@")[1]) == 40
        if step.get("uses", "").startswith("actions/checkout@"):
            assert step["with"]["ref"] == "${{ github.sha }}"
            assert step["with"]["persist-credentials"] is False
