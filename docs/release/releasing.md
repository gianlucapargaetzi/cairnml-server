# Releasing, API changes and how the repositories stay in step

This describes how the server (`cairnml-server`), the SDK (`cairnml`, which includes the agent as its `cairnml_agent`
package) and the web app (`cairnml-web`) are kept consistent and released. The server is the source of truth for the
API. The SDK's client is generated from the server's schema, and the agent uses the SDK.

## Making an API change (server)

The API is defined by the schema in `apiserver/schema/services/*.conf` (one `"2.41"` block per action) and implemented
by the endpoints in `apiserver/services/`. The API level is `API_VERSION` in
`apiserver/service_repo/api_version.py`; a test asserts it is the highest version label in the schema. Each endpoint
is registered at the oldest version label of its action in the schema (`apiserver/service_repo/endpoint.py`), and a
request is served by the newest block at or below the requested version.

1. Change the schema and the endpoint together, on a branch, and open a PR.
2. The **`api-change`** job compares the schema with `main`:
   - an addition (new endpoint, new optional field, new allowed value) passes;
   - a removal or a breaking change (a removed field, a field that became required, a changed type, a value that is no
     longer allowed) fails the PR, unless the API version is raised in the same PR and `CHANGELOG.md` has a section
     `## API <version>` that names the endpoint (the endpoint's name must appear as a whole, not as part of a longer
     name);
   - an endpoint whose request or response differs from its newest definition (in the base branch) fails the PR, even
     when the change is additive, unless the PR adds a definition labelled with a newer version. A comment or a
     description is not part of the contract and never fails.
   - the check also reads changes inside shared definitions, and a field that changes to or from a union of shapes (a
     request that took one shape now takes another) counts as a breaking change.
3. Merge. Locally, the same check runs with `python -m apiserver.contract.evolution --base-ref origin/main`.

### Coordinated work across the repositories

An API change usually needs its implementation in the SDK (with its agent) or the web app. The consumers' CI jobs that read the
server (SDK: `api contract`, which includes the agent's callers; web: `build`, the steps `generate --check` and `check-web`) would be
red against the server's `main` until the server pull request is merged, and the required checks block the consumer
pull request. The convention that lets both be tested together is the branch name:

1. Use the **same branch name** in the server and in every consumer that needs a change (for example `feature/x` in
   `cairnml-server` and in `cairnml`; the prefixes the repositories accept are `ci/`, `feature/`, `build/`,
   `refactor/`, `chore/` and `fix/`).
2. A consumer pull request from branch `X` reads the server's branch `X` when the server has one, else `main`. The
   local action `.github/actions/same-named-ref` of each consumer makes the decision: it looks the branch up with
   `git ls-remote --exit-code --heads` on the public repository, takes the branch name only through `env:` (never as
   shell text), looks up only names made of letters, digits and `. _ / -`, and requires an exact match. No pull request
   event, an unusual name, no such branch or a failed lookup give `main`, exactly as before. The job log says which ref
   was used and why (`same-named-ref: <repository> -> <ref>: <reason>`), and so does the step summary. Pull requests from
   forks are not covered.
3. Regenerate the consumer's code from the server branch (SDK: `scripts/regenerate-api.sh <server branch>`; web: `python -m
   apiserver.client_generator generate --target web --out src/app/business-logic` from the server branch) and commit it
   on the consumer's branch together with the hand-written change.
4. **Merge order: the server first, then the SDK and the web app.** After the server branch is merged and
   deleted, the consumers' CI reads `main`, which has the change, so their pull requests stay green (rerun the check if
   it ran before the server merge). A consumer pull request with a same-named server branch that is merged first is
   checked against a branch that is later deleted: do not do that.
5. The rolling pull requests of the sync workflow (below) are guarded for this: a hand-written commit on
   `chore/api-sync` is never overwritten, so the adaptation of the hand-written code can be committed on that branch.

### What happens in the other repositories

- **After a merge that touches the schema or the generator**, the `sync-sdk-client` workflow keeps the two consumers
  in step. It makes no difference between a breaking change and an addition: any difference in what the generator writes
  counts, because each consumer's check requires its committed code to equal the generator's output.
  - The **SDK** and the **web app** each get ONE pull request, from the rolling branch `chore/api-sync` (the consumers'
    branch-name rule refuses other prefixes; `sync/` is declined with GH013). The branch is rebuilt from the consumer's
    `main` on every run and force-pushed with a lease, and the open pull request is updated in place: its title names
    the latest server commit (`Regenerate the API client from cairnml-server <commit>`, `Regenerate the API layer ...`).
    If a later run finds no difference to `main` any more, it closes that pull request with a comment. A red check on it
    (SDK: `api contract`, web: `check-web` in `build`) means the hand-written code needs updating on that branch: commit
    the update on that branch, the sync will not overwrite it.
  - **Hand-written commits on the rolling branch are never overwritten.** Before it rebuilds the branch, the run fetches
    the remote `chore/api-sync` and lists the commits that are not on the consumer's `main`. When any of them has an
    author email other than the sync's (`release@example.invalid`), the run pushes nothing and closes nothing (also when
    the regenerated output equals `main`), comments ONCE on the open pull request (the comment starts with
    `<!-- api-sync-manual-commits -->`; a rerun finds the marker through the REST comments endpoint and does not repeat
    it) or, when no pull request is open, notes it in the step summary, and the step ends green. The sync resumes when
    the branch is merged, deleted or holds only the sync's own commits again; a merge of `main` made with the "Update
    branch" button counts as a hand-written commit. The push leases on the sha of the branch that was inspected, so a
    commit that arrives later makes the push fail instead of being overwritten. The decision is made by
    `apiserver/contract/manual_commits.py`; the shell only passes files.
  - **What changed in the API:** the body of each pull request has a "What changed in the API"
    section made by `python -m apiserver.contract.evolution --markdown-report`: the API versions, the number of breaking
    changes, one table row per changed endpoint (added, removed or changed, marked **breaking** or additive, with the
    detail and the definition it goes from and to; at most 40 rows, the rest is "and N more"), the problems the contract
    check found, and a link to the changelog section when there is one. "The API contract did not change" means only
    the schema text or the generator changed. The section is ONE cumulative list from a fixed base: the first run of
    a pull request records its base as `<!-- api-sync-base:<sha> -->` in the body, every later run reads it back from the
    open pull request (a marker counts only as a line of its own with a full lower-case sha, and the commit must be an
    ancestor), so the list grows with the pull request; a new pull request after a merge starts from its own base. The
    body is built from files (`apiserver/contract/pr_body.py`), never from schema text in shell, and kept below 60000
    characters by dropping table rows.
  - **Breaking changes:** when the change report has at least one breaking change (the contract tool's definition: a
    removed endpoint, a removed or newly required field, a changed type, a removed allowed value), the workflow opens
    ONE issue with the label `api-breaking` in each of `cairnml` and `cairnml-web`, whether or not
    anything else in that repository changed. The title is `API breaking change <version>: <first five endpoints>[ and N
    more]` (at most 200 characters; the version is the highest one the schema declares). The body names the API versions
    before and after, the server commit, a link to the `## API <version>` changelog section (or "no changelog section"),
    this checklist, and the change list (the section described above):
    - [ ] adopt the new definition where it brings functionality we want;
    - [ ] check that the old definition is still supported by this repository's code;
    - [ ] decide whether anything is dropped.

    There is one issue per API version, ever: a later run for the same version comments on the open issue with the
    new change list, and when the issue is closed the maintainer has worked through it, so nothing is opened or
    commented. The existing issues are looked up through the REST issues endpoint (`gh api --paginate`, pull requests
    left out), and when none matches the version the lookup is repeated once after about 20 seconds before an issue is
    created, so that an issue a run made seconds ago is seen and a quick rerun comments instead of opening a second one.
    The issues are never assigned and never closed automatically; the maintainer closes each one after the
    checklist. An additive change, or no change, opens nothing. The title, body and decision are made by
    `apiserver/contract/breaking_issue.py` from the tool's JSON and markdown reports; the shell only passes files.
  - **The base:** on a push to `main` it is the commit before the push (`github.event.before`); when that is missing,
    all zeros or not an ancestor (a forced push, a new branch), it is the first parent, and the text under the section
    says which. On a `workflow_dispatch` run it is the input `base_ref` (a sha, branch or tag of the server; a name that
    cannot be resolved fails the run), else the first parent of the dispatched commit; an explicit `base_ref` also
    replaces the base an open pull request recorded. The run reads the server's full history for this. If the tool
    cannot read the base or the schema, the run fails before it pushes or opens anything. The concurrency group keeps at
    most one pending run, so a push run that a newer one replaced does not run; the recorded base keeps an existing pull
    request's list complete, but a brand-new pull request after such a drop can miss the changes of the dropped commit
    in its list (the generated diff itself is always complete).
  - The **agent** (`cairnml_agent`, in the SDK checkout) has no generated code. The workflow runs `check-callers --target
    agent --root sdk/cairnml/cairnml_agent --base sdk/cairnml` against the new schema. Findings fail the sync run, and
    the step log lists them; no issue is opened. A failure of the checker itself fails the run.
  - It uses the release GitHub App, installed on `cairnml`, `cairnml-server` and `cairnml-web` with
    write access to contents, pull requests and issues. A run can be started by hand (`workflow_dispatch`); its
    inputs `sdk_ref` and `web_ref` (default `main`) exist to demonstrate the paths on throwaway branches,
    and `base_ref` names the server commit the change list starts from.
    The step summary of a run lists what each consumer got.
- **The SDK's `api contract` job** compares its committed client with what the server generates (the branch named like
  the pull request's branch when the server has one, else `main`; see "Coordinated work"), and checks
  that the hand-written SDK code only calls actions, fields and versions that exist. A red job on the auto-PR (or on
  any SDK PR) means: the server changed, update the SDK. To refresh by hand: `scripts/regenerate-api.sh` in the SDK.
- **The agent's tests and API check** run in the SDK's CI. The agent calls the API through the SDK's generated request
  classes, so an SDK change that breaks the agent shows up in the SDK's own tests. The SDK's `api contract` job runs
  `check-callers --target agent --root cairnml_agent --base sdk/cairnml` against the schema of the server's `main` as
  fetched (or of its branch named like the pull request's branch), on pull requests.
- **The web app's API layer** is generated by the web target and checked by `check-web` (see "The web app's calls and
  their versions" below). The web repository's `build` job (a required check) runs `generate --target web --check`
  and then `check-web --root src/app/business-logic --allow scripts/api-allow.txt`. `check-web` reads the generated
  services and models and the other `src/**/*.ts` files (not HTML or `.js`), against the server's `main` as fetched
  (or its branch named like the pull request's branch), and only on pull requests; the pull request of the sync workflow runs it on the regenerated layer.

None of the CI jobs installs a released package of this project from PyPI.

### Checking the hand-written callers

`python -m apiserver.client_generator check-callers --target <target> --root <folder or file> [--allow <file>]
[--base <folder>]...` checks the Python code that calls the API by hand against the schema. The targets are `sdk` (the SDK's package, checked by the
SDK's `api contract` job with `--root sdk/cairnml --allow sdk/scripts/api-allow.txt`) and `agent` (the agent's package,
checked by the SDK's `api contract` job with `--root cairnml_agent --base sdk/cairnml`). It reports, one line per finding as `path:line: message`:

- a request class of a generated service that the schema does not have, or a keyword that is not a field of it;
- a call with literal `service=` and `action=` that names no action of the schema, or a literal `version=` the action
  does not declare;
- a call that names its service and action as strings, with `send_request`, `get`, `post` or `send` (as `service=` and
  `action=`, or as the first two positional arguments; on any object): when the schema has the action, the report names
  its generated request class (`tasks.EditRequest`), which is to be built and sent instead. A string naming a service or
  action the schema lacks is reported as not in the schema. A forwarding call that takes the service and action from
  variables is not a literal and is not reported. The generated modules are not judged for this;
- a string that is an API version written in hand-written code (`"2.40"`, `"/v2.8/"` in a URL, a `version=` value, a
  `check_min_api_version("2.15")`): the version comes from the generated classes. The generated modules (the ones with
  the line `# Generated by apiserver.client_generator`) are not judged for this. The argument of
  `check_min_server_version` is a server version and is not judged. In a test file (`test_*.py`, `*_test.py`,
  `conftest.py`, or a file under a `tests` folder that is not in a `fixtures` folder) only a `version=` value and a
  `/vX.Y/` in a URL are judged, and not those inside an assertion or a comparison: any other version-like string in a
  test is data (a fake server's level, a compared value, a requirement or an interpreter version);
- a method called on a session object that the `Session` class does not have, inherited methods included. A session
  object is a name `session`, `_session` or `task_session`, an attribute of that name (`self._session`), or a name bound
  to the class `Session`; a name bound to a `requests`, `httpx` or `boto3` object is not one. The methods of
  `requests.Session` are accepted as well, since a session may be a plain HTTP session. The `Session` class is the one
  defined under `--root`, and a base class is resolved through the import that names it: `class Session(SdkSession)`
  with `from cairnml.backend_api.session.session import Session as SdkSession` inherits from the SDK's class, not from
  the class of its own name. The SDK's class is not under the agent's folder, so the agent's check passes the SDK's
  package with `--base <sdk>/cairnml`: it is read for its classes and not checked. Without it the check reports that
  the inherited methods cannot be checked. If the scanned folder has no `Session` class, the check says so;
- an exemption in the allow file (`exempt <service.action> <reason>`) that no call matches any more.

Exit codes: `0` when nothing is reported, `1` when something is reported, `2` for a usage error (a folder that does not
exist, a schema that cannot be read, an allow line without a reason). The allow file exempts a call by its action only.
The check reads source text only; it does not run the code.

The calls that still send a name by hand are listed one action per line in `apiserver/client_generator/allow/sdk.txt`
(target `sdk`) and `allow/agent.txt` (target `agent`), each with its reason. A line goes when its calls are converted: an
exemption that matches no call is reported. The SDK's `api contract` job reads its own `scripts/api-allow.txt`, which
has to take the same lines (cairnml#140).

### The web app's calls and their versions

`python -m apiserver.client_generator generate --target web --out <business-logic folder>` writes the web app's API
layer (`api-services/`, `model/<service>/`). Every call is written at the version of its definition, read from the
schema: `${this.basePath}/v<version>/<service>.<action>`, never a fixed version. The target also writes
`api-services/endpoints.ts`, the version of every action of the schema and `endpointUrl(action)`, which returns the URL
of an action at that version and throws for an action the schema does not have; hand-written web code that needs an
address uses it instead of writing a version. `generate --check` reports a file that differs from the schema.

`python -m apiserver.client_generator check-web --root <business-logic folder> [--allow <file>]` checks the hand-edited
`api-services/*.service.ts` against the schema: a call to an action the schema lacks, a call without a version, a call
at a version other than the one of its definition, and a field of a request or response model the schema does not have.

`check-web` also reads the web app's hand-written sources, the `.ts` files under `--src` (default: the folder named `src`
above `--root`, or `--root` itself when there is none), skipping `*.spec.ts` and the generated `api-services/` and
`model/` folders of `--root`. It reports a request path written by hand, `/<service>.<action>` (optionally behind
`/v<major>.<minor>/`), for an action of the schema, with the generated function to call instead
(`ApiTasksService.tasksEdit`); and a `/v<major>.<minor>/` literal, whose address comes from the generated functions
(`endpointUrl(action)`). An action named as a string in `endpointUrl('<service>.<action>')` is reported too, with the
generated function to call instead (`endpointUrl('access.share')` gives `ApiAccessService.accessShare`). The unversioned
login actions (`UNVERSIONED_ACTIONS`, such as `endpointUrl('login.logout')`) are not reported. Exceptions go in the same
allow file, `exempt <service.action> <reason>`.

A name that the schema lacks is reported too, with the closest action of the schema as a hint:
`endpointUrl('acess.share')` gives `acess.share is not an action of the server's schema: the likely intended action is
access.share`, and the literal `/cairnml/api/tasks.edti` gives the hint `tasks.edit`. Only these two forms are judged for
an unknown name: an endpointUrl argument, and a path under the `/api/` prefix (optionally behind `/v<major>.<minor>/`). A
file name, a relative import, a URL of another host or a number is never taken for an action.

**Options of a call.** Every generated method takes the options of its call as its last argument (`ApiOptions`):
`headers` are added to the request's headers (a tenant header, for example), and `params` are appended to its query.
A JSON call is a POST with the request as its body, as before. Two kinds of action are not JSON, and their rules are
listed in `apiserver/client_generator/web_renderer.py` with the reason, as `UNVERSIONED_ACTIONS` is:

- `FILE_ACTIONS`: the response is a file. The method returns `Observable<Blob>` and reads the response as a file (no
  JSON envelope, no JSON `Accept` header). `events.download_task_log` and `organization.download_for_get_all` are such.
- `GET_ACTIONS`: the call is a GET, and the request's fields are the query of its address. The class also gets
  `<service><Action>Url(request)`, the address of that call, which a download link follows (the browser cannot send a
  POST body for a link). `organization.download_for_get_all` is such.

The rules are checked against the schema: a rule that names an action the schema lacks makes `generate` fail. The rules
do not change the schema, so they are not an API change: the server serves these actions as they are.

**Calls without a version.** A path without a version is served with the newest definition of the action (the server
takes the newest API version when the path has none). Three actions are called without one, on purpose, listed in
`UNVERSIONED_ACTIONS` in `apiserver/client_generator/web_renderer.py` with the reason:

- `login.logout`: the ID-token cookie (`apiserver.auth.custom_cookies.idp_session` in `apiserver.conf`) has the path
  `/cairnml/api/login.logout`. A browser sends a cookie only on a request whose path starts with its Path, so a call at
  `/cairnml/api/v<version>/login.logout` would not carry it and the identity provider would ask to confirm the logout.
  The web app therefore calls `${basePath}/login.logout`, without a version. A unit test ties the cookie path in the
  config to this rule.
- `login.idp_start` and `login.idp_callback`: the identity provider keeps these addresses (the realm lists the
  callback), and the server hands them out without a version (`idp/public_origin.py`).

For these, `check-web` reports a call that carries a version, and the generator writes the unversioned path. A new
action that is bound to a path (a cookie `path`, an address kept by another system) is added to that list in the same
change as the path.

## Releasing

Each repository has a version file: `apiserver/version.py` (server), `cairnml/version.py` (SDK, which includes the agent),
and `package.json` (web app).

**A repository is releasing when its version file is higher than its latest release tag and there are commits since.**
Otherwise it is staying: it is used at its latest release tag. Unreleased commits without a bump are only reported.

### Steps

1. Bump the version file in each repository you want to release, through a PR, and merge it.
2. In the server repository: **Actions → release → Run workflow**, on `main`. Nothing is typed in.
3. The workflow runs, in order:
   1. **check** (`release-check.yml`): works out what is releasing, and tests the server and the SDK together at exactly
      those commits (a releasing repo at its `main`, a staying repo at its latest release tag): the server's unit tests
      with the SDK, and the SDK's generated client against the server's schema plus its tests. It also refuses a version that is already tagged or on PyPI, or lower than the last release, and a release
      that is not started from `main`. If it fails, or nothing is releasing, the run stops and **no tag is created**.
   2. **approve**: you approve the plan (the `release` environment).
   3. **server**: tags and releases the server, if it is releasing.
   4. **SDK**: releases the planned commit under the version file's version, if releasing. It starts the SDK's `publish`
      workflow, which waits for the `pypi` approval and refuses to upload unless its gate passes (the client equals what
      the newest released server tag generates).
   5. **web app**: releases its planned commit with its CHANGELOG section as the release notes, if releasing.
   6. **image**: if the server is releasing, builds the server image from the server tag with the SDK built from the
      planned commit, and pushes it after the `dockerhub` approval. No pin file is involved.

Examples: a server-only bug fix: bump `apiserver/version.py` only; the SDK is tested at its last release.
A new SDK release against the current server: bump `cairnml/version.py` only; it is tested against the latest released
server.

### Release notes (CHANGELOG.md)

A section of a changelog is read by the users of the release (a person using the web app, a developer using the SDK or the agent, someone running the server), who see it in the "What's new" dialog, so it says what they can now do or must change, in the words of their documentation, and never how the release was made, checked or planned.

- A releasing component must have its `## <version>` section in its `CHANGELOG.md` (`release-check` refuses the release without it).
- The image carries the "What's new" bundle: the sections of the server, the web app and the SDK, made by `apiserver/release/whats_new.py` while the image is built, and the web app shows the newest section of each to a user who has not seen it. Write one line per bullet and no markdown: the dialog shows each line as a paragraph and shows markdown as text.
- The web app's release uses its section as the text of the GitHub release; the server and the SDK use generated notes.
- What belongs in the maintainer's notes (the API change check, how a release is planned) is in this file, above and below.

### Trying it without releasing

**Actions → release-check → Run workflow** (server repository) prints the plan (`RELEASING` or `staying` per
repository) and runs the tests. It publishes nothing.

### If something fails halfway

The tag of an earlier step stays: if the server was released and the SDK upload failed, run the workflow again. The
server is now at its latest tag, so it is staying; the SDK is still bumped, so it is releasing.

## One-time setup

- A GitHub App (permissions: Contents, Actions, Pull requests: read and write), installed on `cairnml-server`, `cairnml`
  and `cairnml-web`. Its ID and private key are the repository secrets `RELEASE_APP_ID` and `RELEASE_APP_KEY` of the
  server repository. A release made with the default token would not start the publish workflows.
- Environments in the server repository: `release` (required reviewer: you) and `dockerhub`. In the SDK repository:
  `pypi` (required reviewer) and `testpypi`.

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
| Web | `build` | PRs and push |
