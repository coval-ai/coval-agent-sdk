# Releasing

The initial release candidate is `0.1.0`. [Release Automation](../.github/workflows/release-automation.yml) uses pinned Python Semantic Release tooling to derive versions and generate `CHANGELOG.md` from Conventional Commits. Its `allow_zero_version` configuration supports the initial `0.1.0` release. The version in `pyproject.toml` supplies package metadata; contributors do not update it manually.

## Contributor flow

Squash-merge each reviewed PR into `main` using its Conventional Commit title. `fix:` and `perf:` produce a patch, `feat:` produces a minor, and `!` or `BREAKING CHANGE:` produces a major, including `1.0.0` from a `0.x` version. Maintenance and documentation commits do not release on their own. The first merged SDK feature provides the initial minor-release signal for `0.1.0`.

Release Automation checks out current `main`, computes the next version, regenerates `CHANGELOG.md`, and creates a local `chore(release): X.Y.Z` commit and matching `vX.Y.Z` tag. It validates that only the changelog and package version changed, then pushes the commit and tag atomically using the Coval Release Automation GitHub App. Concurrent changes to `main` cause validation or the non-fast-forward push to fail rather than overwrite work. The App's own commit does not start another preparation run.

The tag starts [the publishing workflow](../.github/workflows/publish.yml). It requires an App-created tag on a generated release commit in `main` history, a matching package version, and a generated changelog entry. It then runs `scripts/check`, builds the distributions, verifies the exact release wheel in a clean base environment, and transfers the artifacts to the isolated `pypi` publishing job.

Once configured, a releasable merge can therefore lead to a public PyPI upload without a manual version bump or tag. Configure environment approval if maintainers need to approve each upload. `scripts/build` only creates local artifacts. Repository visibility is independent of package publication: a private GitHub repository can publish publicly downloadable Python code.

## Configure publishing once

Install the existing Coval Release Automation GitHub App on this repository with Contents write access. Make `RELEASE_AUTOMATION_APP_CLIENT_ID` available as a repository variable and `RELEASE_AUTOMATION_APP_PRIVATE_KEY` as a repository secret through the existing maintainer process. The App must be allowed to push its generated commit to `main` under the applicable branch rules. Its installation token also allows tag pushes to start the publishing workflow; GitHub suppresses follow-on workflow triggers for tags pushed with the default `GITHUB_TOKEN`.

Use the intended PyPI owner account or organization and establish maintainer access. For a new project, register a [pending Trusted Publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/) under the owner's account publishing settings:

| Setting | Value |
| --- | --- |
| PyPI project | `coval-agent-sdk` |
| GitHub owner | `coval-ai` |
| Repository | `coval-agent-sdk` |
| Workflow filename | `publish.yml` |
| Environment | `pypi` |

A pending publisher creates the project on its first successful upload; it does not reserve the package name. If the project already exists, its owner must add this Trusted Publisher in the project's publishing settings instead. Confirm the resulting project ownership and maintainer access after the first upload.

Create the GitHub environment `pypi` and configure its protection rules before releasing. Require maintainer review where the repository's GitHub plan supports it, and restrict deployment tags to `v*`. Match the environment name exactly in GitHub and PyPI. This setup is a maintainer action; adding the workflow file does not configure either service.

The separate publish job uses [Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/) with job-scoped `id-token: write` and PyPA's publishing action. Do not add a stored PyPI API token. The build job has read-only repository permissions and cannot request an OIDC token. The publish job downloads the build's distributions without checking out or executing the SDK and includes provenance attestations.

## Before the first public release

- Settle the public API, audio contract, error behavior, and intended version.
- Review the source, Git history, licenses, notices, examples, and vulnerability-reporting route for the intended public audience.
- Complete the App, publisher, and environment setup above before merging changes that should release.
- Require the intended branch checks and squash-merge convention; workflow files do not configure branch protection or merge settings.

The source declares `0.1.0` as the initial candidate. The automation has no release tag until it creates one; source metadata, generated changelog entries, and Git tags do not establish that PyPI accepted an upload.

## Validation and recovery

Use Python 3.12 for maintainer tooling:

```sh
scripts/install
.venv/bin/python -m pytest release_tests -q
scripts/check
scripts/build
```

The release suite exercises the real pinned release tool in isolated Git repositories, including initial, patch, minor, breaking, and no-op cases. Ordinary runtime CI still checks Python 3.10, 3.12, and 3.14; release-tool tests run separately on Python 3.12. The publishing workflow runs the package checks on Python 3.12, without waiting on or changing branch-check settings.

If preparation fails before the atomic push, rerun `Release Automation` manually from `main`. It considers all commits since the latest release and does nothing when there is no unreleased semantic change. Do not increment a version merely to retry preparation.

If preparation succeeded but the package upload failed, fix the publisher/environment problem and rerun the failed publishing job for the same generated tag. A preparation retry will not recreate an existing version. Do not move tags, replace published artifacts, or use a manually pushed tag to bypass the release contract. If the package was uploaded successfully and needs a correction, merge another conventionally titled fix to release a new version.

After publishing, verify the version and provenance on PyPI, install the published package into a fresh consumer environment, and exercise the relevant integration. Update installation documentation only after this evidence exists. The automatically generated changelog describes prepared releases; PyPI and successful consumer installation establish package availability.
