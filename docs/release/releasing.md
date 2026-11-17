# Releasing, API changes and how the repositories stay in step

This describes how the server (`cairnml-server`), the SDK (`cairnml`), the agent (`cairnml-agent`) and the web app
(`cairnml-web`) are kept consistent and released. The server is the source of truth for the API. The SDK's client is
generated from the server's schema, and the agent uses the SDK.

## Making an API change (server)

The API is defined by the schema in `apiserver/schema/services/*.conf` (one `"2.41"` block per action) and implemented
by the endpoints in `apiserver/services/`. The served version is `API_VERSION` in
`apiserver/service_repo/api_version.py`.

1. Change the schema and the endpoint together, on a branch, and open a PR.
2. The **`api-change`** job compares the schema with `main`:
   - an addition (new endpoint, new optional field, new allowed value) passes;
   - a removal or a breaking change (a removed field, a field that became required, a changed type, a value that is no
     longer allowed) fails the PR, unless the API version is raised in the same PR and `CHANGELOG.md` has a section
     `## API <version>` that names the endpoint.
3. Merge. Locally, the same check runs with `python -m apiserver.contract.evolution --base-ref origin/main`.

### What happens in the other repositories

- **After a merge that touches the schema or the generator**, the `sync-sdk-client` workflow regenerates the SDK's
  generated client and opens a PR in the SDK repository (`Regenerate the API client from cairnml-server <commit>`). It
  uses the GitHub App. If nothing changed, it does nothing.
- **The SDK's `api contract` job** compares its committed client with what the server's `main` generates, and checks
  that the hand-written SDK code only calls actions, fields and versions that exist. A red job on the auto-PR (or on
  any SDK PR) means: the server changed, update the SDK. To refresh by hand: `scripts/regenerate-api.sh` in the SDK.
- **The agent's CI** installs the SDK's `main` from GitHub, so an SDK change that breaks the agent shows up there. The
  agent calls the API through the SDK's generated request classes.
- **The web app** is not covered yet: its services are hand-written and there is no generator or API check.

None of the CI jobs installs a released package of this project from PyPI.

## Releasing

Each repository has a version file: `apiserver/version.py` (server), `cairnml/version.py` (SDK),
`cairnml_agent/version.py` (agent).

**A repository is releasing when its version file is higher than its latest release tag and there are commits since.**
Otherwise it is staying: it is used at its latest release tag. Unreleased commits without a bump are only reported.

### Steps

1. Bump the version file in each repository you want to release, through a PR, and merge it.
2. In the server repository: **Actions → release → Run workflow**, on `main`. Nothing is typed in.
3. The workflow runs, in order:
   1. **check** (`release-check.yml`): works out what is releasing, and tests the three together at exactly those
      commits (a releasing repo at its `main`, a staying repo at its latest release tag): the server's unit tests with
      the SDK, the SDK's generated client against the server's schema plus its tests, and the agent's tests with the
      SDK. It also refuses a version that is already tagged or on PyPI, or lower than the last release, and a release
      that is not started from `main`. If it fails, or nothing is releasing, the run stops and **no tag is created**.
   2. **approve**: you approve the plan (the `release` environment).
   3. **server**: tags and releases the server, if it is releasing.
   4. **SDK**, then **agent**: release the planned commit under the version file's version, if releasing. Each starts
      that repository's `publish` workflow, which waits for the `pypi` approval and refuses to upload unless its gate
      passes (SDK: the client equals what the newest released server tag generates; agent: the newest released SDK is
      accepted by `requirements.txt`).
   5. **image**: if the server is releasing, builds the server image from the server tag with the SDK and the agent
      built from the planned commits, and pushes it after the `dockerhub` approval. No pin file is involved.

Examples: a server-only bug fix: bump `apiserver/version.py` only; the SDK and agent are tested at their last releases.
A new SDK release against the current server: bump `cairnml/version.py` only; it is tested against the latest released
server.

### Trying it without releasing

**Actions → release-check → Run workflow** (server repository) prints the plan (`RELEASING` or `staying` per
repository) and runs the tests. It publishes nothing.

### If something fails halfway

The tag of an earlier step stays: if the server was released and the SDK upload failed, run the workflow again. The
server is now at its latest tag, so it is staying; the SDK is still bumped, so it is releasing.

## One-time setup

- A GitHub App (permissions: Contents, Actions, Pull requests: read and write), installed on `cairnml-server`, `cairnml`
  and `cairnml-agent`. Its ID and private key are the repository secrets `RELEASE_APP_ID` and `RELEASE_APP_KEY` of the
  server repository. A release made with the default token would not start the publish workflows.
- Environments in the server repository: `release` (required reviewer: you) and `dockerhub`. In the SDK and agent
  repositories: `pypi` (required reviewer) and `testpypi`.

## The pipelines

| Repository | Workflow | Runs |
|---|---|---|
| Server | `ci` (test, api-change, ruff) | PRs; push to `main` (api-change and ruff on PRs only) |
| Server | `docker` | PRs: build and scan; started by the release: scan and push |
| Server | `stack`, `security` | PRs and push; security also on a schedule |
| Server | `release-check`, `release` | by hand |
| Server | `sync-sdk-client` | push to `main` that changes the schema or the generator |
| SDK | `ci` (test, api contract, lint, versions, package, ...) | PRs and push |
| SDK | `publish` | started by the release (or by hand) |
| Agent | `ci` | PRs and push |
| Agent | `publish` | started by the release (or by hand) |
| Web | `build` | PRs and push |
