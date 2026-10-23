"""Exchange generated commits as data between preparation and promotion."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess

RELEASE_FILES = {"CHANGELOG.md", "pyproject.toml"}
FINAL_TAG = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
REVISION = re.compile(r"[0-9a-f]{40}")


def _git(*arguments: str) -> bytes:
    return subprocess.check_output(  # noqa: S603 -- Fixed Git executable.
        ["git", "-c", "core.hooksPath=/dev/null", *arguments],  # noqa: S607
    )


def _text(*arguments: str) -> str:
    return _git(*arguments).decode().strip()


def _identity() -> list[str]:
    name = "Coval Release Automation"
    email = "coval-release-automation[bot]@users.noreply.github.com"
    return [
        "-c",
        f"user.name={name}",
        "-c",
        f"user.email={email}",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "tag.gpgsign=false",
    ]


def _require_context(kind: str, tag: str, source: str) -> None:
    if kind != "release" or not FINAL_TAG.fullmatch(tag) or source:
        raise ValueError("A release requires one final version tag.")


def _read_regular(path: pathlib.Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise ValueError("Candidate files must be bounded regular files.")
        contents = stream.read(limit + 1)
        if len(contents) > limit:
            raise ValueError("Candidate file exceeds its size limit.")
        return contents


def export_candidate(
    directory: pathlib.Path, kind: str, tag: str, source: str
) -> None:
    """Export the last local commit without granting it remote write access.

    Args:
        directory: New artifact directory outside the checkout.
        kind: Release metadata; other candidate types are rejected.
        tag: Final release tag.
        source: Must be empty for a release.
    """
    _require_context(kind, tag, source)
    if _text("status", "--porcelain"):
        raise ValueError("Candidate export requires a clean checkout.")
    base = _text("rev-parse", "HEAD^")
    patch = _git(
        "diff", "--binary", "--no-ext-diff", "--no-renames", base, "HEAD"
    )
    directory.mkdir(mode=0o700, parents=True)
    (directory / "candidate.patch").write_bytes(patch)
    manifest = {
        "schema_version": 1,
        "kind": kind,
        "base": base,
        "tag": tag,
        "source": source,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
    }
    (directory / "candidate.json").write_text(json.dumps(manifest))


def _check_index() -> None:
    paths = (
        _git("diff", "--cached", "--name-only", "-z").decode().split("\0")[:-1]
    )
    if not paths:
        raise ValueError("A candidate must contain a change.")
    for path in paths:
        allowed = path in RELEASE_FILES
        if not allowed:
            raise ValueError(f"Candidate changed an unauthorized path: {path}")
    entries = _git("ls-files", "--stage", "-z").decode().split("\0")
    changed = set(paths)
    for entry in filter(None, entries):
        metadata, path = entry.split("\t", 1)
        if path in changed and metadata.split()[0] not in {"100644", "100755"}:
            raise ValueError("Candidate changes must use regular files.")


def prepare_candidate(
    directory: pathlib.Path,
    kind: str,
    tag: str,
    source: str,
    expected_base: str,
) -> None:
    """Validate artifact data and prepare a commit before token creation.

    Args:
        directory: Downloaded data artifact directory outside the checkout.
        kind: Expected candidate type from the trusted workflow.
        tag: Expected final tag.
        source: Must be empty for a release.
        expected_base: Protected-main revision from the workflow event.

    Raises:
        ValueError: Integrity, source, file policy, or freshness checks fail.
    """
    _require_context(kind, tag, source)
    if not REVISION.fullmatch(expected_base):
        raise ValueError("A resolved workflow revision is required.")
    if _text("status", "--porcelain"):
        raise ValueError("Promotion requires a clean checkout.")
    manifest = json.loads(_read_regular(directory / "candidate.json", 16384))
    patch = _read_regular(directory / "candidate.patch", 64 * 1024 * 1024)
    expected = {
        "schema_version": 1,
        "kind": kind,
        "base": expected_base,
        "tag": tag,
        "source": source,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
    }
    if manifest != expected:
        raise ValueError(
            "Candidate integrity or source identity did not match."
        )
    _git("fetch", "origin", "main:refs/remotes/origin/main")
    if not (
        _text("rev-parse", "HEAD")
        == _text("rev-parse", "origin/main")
        == expected_base
    ):
        raise ValueError("Main changed; prepare a new candidate.")
    subprocess.run(
        ["git", "apply", "--cached", "--whitespace=nowarn", "-"],  # noqa: S607
        input=patch,
        check=True,
    )
    _check_index()
    deleted = (
        _git("diff", "--cached", "--diff-filter=D", "--name-only", "-z")
        .decode()
        .split("\0")[:-1]
    )
    for path in deleted:
        pathlib.Path(path).unlink()
    _git("checkout-index", "--all", "--force")
    _git(*_identity(), "commit", "-m", f"chore(release): {tag[1:]}")
    _git(*_identity(), "tag", "--annotate", tag, "--message", tag)
    if _text("status", "--porcelain"):
        raise ValueError("Promotion left unexpected checkout changes.")


def main() -> None:
    """Read a candidate command without accepting executable artifact inputs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("export", "prepare"))
    parser.add_argument("kind", choices=("release",))
    parser.add_argument("directory", type=pathlib.Path)
    parser.add_argument("--tag", default="")
    parser.add_argument("--source", default="")
    parser.add_argument("--base", default="")
    args = parser.parse_args()
    if args.operation == "export":
        export_candidate(args.directory, args.kind, args.tag, args.source)
    else:
        prepare_candidate(
            args.directory, args.kind, args.tag, args.source, args.base
        )


if __name__ == "__main__":
    main()
