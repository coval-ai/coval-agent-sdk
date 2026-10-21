# Releasing

This maintainer guide describes versioning and publication of `coval-agent-sdk`. [Release Automation](../.github/workflows/release-automation.yml) uses pinned Python Semantic Release tooling to derive versions and generate `CHANGELOG.md` from Conventional Commits. Contributors do not edit the package version, changelog, or release tags manually.

## From a pull request to PyPI

Squash-merge a reviewed PR into `main` using its Conventional Commit title:

| Change | Version effect |
| --- | --- |
| `fix:` or `perf:` | Patch |
| `feat:` | Minor |
| `!` or `BREAKING CHANGE:` | Major, including `1.0.0` from `0.x` |
| `docs:`, `chore:`, and other types | No release on their own |

Release Automation checks out current `main`, computes the next version, regenerates the changelog, and creates a local `chore(release): X.Y.Z` commit and `vX.Y.Z` tag. It validates that only the changelog and package version changed, then pushes both atomically using a GitHub App installation token. Concurrent changes to `main` fail validation or the non-fast-forward push rather than overwrite work. The App's own commit does not start another preparation run.

The tag starts [Publish](../.github/workflows/publish.yml). It requires an App-created tag on a generated release commit in `main` history, a matching package version, and a generated changelog entry. It runs `scripts/check`, builds distributions, verifies the release wheel in a clean base environment, and passes the artifacts to an isolated `pypi` job for upload with provenance attestations.

A releasable merge can publish to PyPI automatically; GitHub environment approval applies when configured. `scripts/build` creates local artifacts only. Package publication and repository visibility are separate settings.

## Updating the PyPI description

PyPI displays the README included in each uploaded distribution. Editing the README on GitHub does not change the description of an already published version. Correct packaged documentation or metadata with a reviewed `fix(docs): ...` PR when a patch release is intended. Confirm any new documentation URLs are live before releasing. Ordinary `docs:` PRs wait for a later releasable change.

Do not reuse a published version number or replace an existing artifact to correct its description. Let automation create a new version containing the corrected README.

## Publishing configuration

Repository maintainers manage the GitHub App installation, branch rules, `pypi` environment, and PyPI Trusted Publisher. The App needs Contents write access and permission to push generated release commits under the applicable branch rules. The workflows use repository variable `RELEASE_AUTOMATION_APP_CLIENT_ID` and secret `RELEASE_AUTOMATION_APP_PRIVATE_KEY`. Keep private keys in GitHub's secret storage. Installation-token pushes allow follow-on workflows to run; tags pushed with the default `GITHUB_TOKEN` do not trigger them.

The PyPI project uses this [Trusted Publisher](https://docs.pypi.org/trusted-publishers/using-a-publisher/) identity:

| Setting | Value |
| --- | --- |
| PyPI project | `coval-agent-sdk` |
| GitHub owner | `coval-ai` |
| Repository | `coval-agent-sdk` |
| Workflow filename | `publish.yml` |
| Environment | `pypi` |

Manage this publisher in the existing PyPI project's publishing settings. Keep the GitHub environment name identical, restrict its deployment tags to `v*`, and require maintainer approval where the repository's GitHub plan supports it. Workflow files do not configure environment rules, branch protection, squash merging, or PyPI ownership.

The publish job uses job-scoped `id-token: write` and PyPA's publishing action. It downloads the checked artifacts without checking out or executing SDK code. The build job has read-only repository permissions and cannot request an OIDC token. Do not add a stored PyPI API token.

## Validation and recovery

Use Python 3.12 for maintainer tooling:

```sh
scripts/install
.venv/bin/python -m pytest release_tests -q
scripts/check
scripts/build
```

The release suite exercises the pinned release tool in isolated Git repositories, including initial, patch, minor, breaking, and no-op cases. Runtime CI checks Python 3.10, 3.12, and 3.14; release-tool tests run separately on Python 3.12. The publishing workflow runs package checks on Python 3.12.

If preparation fails before the atomic push, rerun Release Automation manually from `main`. It considers commits since the latest release and does nothing when there is no unreleased semantic change. Do not increment a version merely to retry preparation.

If preparation succeeded but upload failed, fix the publisher or environment problem and rerun the failed publishing job for the same generated tag. A preparation retry does not recreate an existing version. Do not move tags, replace artifacts, or push a manual tag to bypass validation. If the package was already uploaded, release a new version containing the correction.

After publishing, verify the version and provenance on PyPI, install it into a fresh consumer environment, and exercise the affected integration. A generated changelog or successful local build alone does not prove package availability.
