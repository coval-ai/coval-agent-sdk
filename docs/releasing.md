# Releasing

The initial release candidate is `0.1.0`. The version in `pyproject.toml` is authoritative; it supplies distribution metadata and artifact filenames. `0.1.0` is a final version in Python packaging terms, without a `1.0` API stability promise. Until the first upload succeeds, this remains an unpublished candidate.

`scripts/build` creates local artifacts only. [The publishing workflow](../.github/workflows/publish.yml) publishes to public PyPI when a maintainer pushes a matching `v` version tag, such as `v0.1.0`. Merging code or changing the version alone does not publish. Package publication and repository visibility are separate actions; a private GitHub repository can publish publicly downloadable Python code.

## Configure publishing once

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

- Settle the public API names, audio connection contract, error behavior, and intended version.
- Review the repository and Git history for credentials, customer material, private links, and copied material requiring attribution.
- Confirm ownership, `LICENSE`, `NOTICE`, SPDX metadata, and any third-party notices.
- Confirm a working private vulnerability-reporting channel and update `SECURITY.md` with its actual route.
- Confirm that README examples and installation instructions work for the intended release audience.
- Complete the publisher and environment setup above.
- Review CI and branch protection settings; a workflow file alone does not make a check required for merging.

These are release criteria, not a claim that the repository is already public or a package version has been published.

## Build the candidate

1. Update `pyproject.toml`, affected wheel examples, and `CHANGELOG.md` for the intended version. Keep changes under `Unreleased` until publication; do not invent a release date or registry badge.
2. Run `scripts/check` on the supported CI Python versions when a release or compatibility change warrants that matrix.
3. Start with an empty `dist/` directory so old versions cannot be mistaken for the candidate, then run `scripts/build`.
4. Inspect the source archive and wheel for the package, `py.typed`, metadata, absence of executable entry points, `LICENSE`, and `NOTICE`, with no tests or development artifacts shipped as importable packages.
5. Install the wheel into a clean consumer environment and exercise the relevant Python API. Record any authorized hosted check separately from offline evidence.
6. Merge the reviewed candidate and release workflow to `main` before tagging.

## Publish an authorized release

Only run these commands when publication of that version and commit is explicitly authorized. For the initial `0.1.0` release:

```sh
git fetch origin main:refs/remotes/origin/main
git tag -a v0.1.0 origin/main -m "Release 0.1.0"
git push origin refs/tags/v0.1.0
```

The tag push starts the workflow at that commit. It rejects a tag that differs from `v` plus the package version or a commit outside `main`, runs `scripts/check` on Python 3.12, builds the wheel and source archive with `scripts/build`, and checks the exact release wheel in a clean base environment. It transfers those distributions to the `pypi` environment for upload. Ordinary CI continues to validate Python 3.10, 3.12, and 3.14; the release workflow does not wait for or change those branch checks.

After the publish job succeeds, verify the version and provenance on PyPI, install the published package into a fresh consumer environment, and exercise the relevant integration. Update the changelog and installation documentation to describe the published release only after that evidence exists. Future releases need a new version; do not move release tags or overwrite a published artifact.
