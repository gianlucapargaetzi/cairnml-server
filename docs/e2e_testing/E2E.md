# End-to-end tests

The end-to-end suite checks the framework the way a person uses it: the stack runs, machines are enrolled through the
real sign-in page, runs are recorded, artifacts and datasets move between machines, agents run jobs, and the web app
shows all of it. Every test is listed, with what it checks, in [E2E_TESTS.md](E2E_TESTS.md).

The suite lives in `tests/e2e/`. It starts **its own stack** and runs against that. It does not use the stack under
`deploy/`, and it does not read `deploy/.env`.

## Running the suite

The suite runs in an isolated environment (see [The isolated environment](#the-isolated-environment)). The host starts one
container, `dind`; the test runner, the stack and the job containers run inside it.

```bash
tests/e2e/docker/run.sh                        # the whole suite
tests/e2e/docker/run.sh -k artifacts           # one area (module or test name)
tests/e2e/docker/run.sh test_09_jobs.py -v     # one module
tests/e2e/docker/run.sh -n 4 -m "not restart"  # in parallel, then the restart test on its own:
tests/e2e/docker/run.sh -m restart
tests/e2e/docker/run.sh --stop-stack           # stop the CairnML stack, keep the environment
tests/e2e/docker/run.sh --down                 # remove the environment: containers, images and volumes
```

- **Reuse.** The stack keeps running between runs. The next run finds it healthy and starts at once; it is rebuilt only
  when the server or web sources, or the compose file, changed. At the start of each run the suite resets what the
  earlier runs left (`system.e2e_reset`, below). Parallel runs need reuse, and the environment turns it on.
- **Report.** Every run writes `tests/e2e/reports/e2e-report.html` and `e2e-report.md`: one row per test, with its name,
  what it checks, what it expects, its outcome, pass or fail, and how long it took. A test's expectation is the `Expect:`
  paragraph of its docstring; a test without one is listed as not written. The runner runs as the user who starts
  `run.sh`, so the folder is theirs on the host.
- **Markers** (`tests/e2e/pytest.ini`): `agent`, `docker`, `ui`, `github`, `slow`, `restart`, `xdist_group`.
- **GitHub tests.** `CAIRNML_E2E_GITHUB_REPO=owner/name` names a repository on github.com that the GitHub App can read;
  `CAIRNML_E2E_GITHUB_PRIVATE_REPO=owner/name` names one it cannot. Without them, the `github` tests are skipped with that
  reason.

`system.e2e_reset` removes the projects, tasks and workers whose name starts with the suite's prefix (`e2e`),
with their contents. It is off unless the server's `apiserver.e2e.reset_enabled` is set, which only the suite's compose
file does, and it requires an admin.

### Running on the host (not the isolated environment)

Use this only where the isolated environment cannot run. The suite then starts its stack on the host's Docker daemon,
on 127.0.0.1 and nine free ports in 18000 to 18999. Run one suite at a time, and not on a machine with a live agent of
the deploy stack (see Limits). Needs Docker with Compose v2, uv, and Python 3.12.

```bash
# 1. Sources, in a scratch folder: the server at the commit under test, and the SDK, the agent and the web app at the
#    commits their pin files name (scripts/fetch-sources.sh puts them in cairnml/, cairnml-agent/ and web/).
# 2. The suite's environment: its exact pins, and the SDK and the agent from their folders.
uv venv -p 3.12 .venv
uv pip install -r tests/e2e/requirements.txt -e "./cairnml[dataset]" -e ./cairnml-agent
.venv/bin/python -m playwright install chromium
# 3. The suite, from a folder that is neither the SDK's nor the agent's.
cd tests/e2e && CAIRNML_ES_HEAP=1g ../../.venv/bin/pytest -v
```

`CAIRNML_ES_HEAP` caps Elasticsearch's heap (the default is 2g). The stack's state folder is in `TMPDIR`. The report is
written to `tests/e2e/reports/`. `tests/e2e/requirements.txt` holds the exact pins the runner image uses too; its header
says how to regenerate them.

## The isolated environment

The host runs one container, `dind`: a Docker daemon of its own. Everything the suite uses runs inside it: the test
runner, the CairnML stack, the managed agent, and the agent's job containers. The host's Docker daemon starts `dind` and
nothing else, and nothing inside it is published to the host.

| Where | What |
|---|---|
| host | `docker compose -f tests/e2e/docker/compose.yaml up dind`, then `inside.sh` runs in it. Nothing else. |
| `dind` (privileged) | Its daemon runs every container below. Its filesystem holds `/workspace` (the repository, mounted from the host), `/e2e-state` and `/e2e-cache` (volumes), and the images it pulls and builds. |
| runner (in `dind`) | Built by `inside.sh` from `runner.Dockerfile`: the test venv (the suite, the SDK and the agent from their folders, editable), Chromium, the Docker CLI and Compose, git. It runs `pytest`, the managed agent and the browser. |
| stack (in `dind`) | The CairnML stack from `stack/compose.yaml`, started by the suite with the Compose plugin of the runner, against the daemon of `dind`. |
| job containers (in `dind`) | Started by the agent, through the same daemon. |

**Networking.** The runner starts with `--network host`: it shares the network namespace of `dind`, as the stack does. So
the stack is reached at `127.0.0.1`, the browser and the SDK use the same addresses, and a job container reaches the
server through the loopback rule of the agent (`cairnml-agent/cairnml_agent/commands/worker.py`, host networking for a
loopback server).

**Paths.** The runner mounts the daemon's socket, so its `docker` and `docker compose` commands go to the daemon of `dind`.
The runner's paths and the daemon's paths are the same filesystem: `/e2e-state` (the suite's state, temp files and the
agent's caches), `/e2e-cache` and `/workspace` are the same folders for both, so a folder the suite or the agent creates
can be bind-mounted by a container of that daemon.

**Why this shape.** The stack and its job containers share one daemon, which is the one `run.sh --down` throws away. The
runner's image can change without touching the stack. The host runs one container, and the host's Docker daemon is not
reachable from anything inside `dind`.

**Costs and risks.**
- `dind` is privileged. Its daemon is root for itself only; the host's daemon is not reachable from inside.
- The first run pulls every image into `dind` (Elasticsearch, Keycloak, MongoDB, Redis, RustFS, `python:3.11-slim`,
  `node:24`, `alpine`, `python:3.12-slim`, the Chromium build). That is several gigabytes, once, in `e2e-dind-data`.
- The runner image is built in `dind` on each run; its layers are cached, so only changed sources rebuild.
- A `dind` container that was restarted can keep a stale `containerd` from its last life, and its daemon then refuses to
  start. Recreating the container (`docker compose up -d --force-recreate dind`) clears it; the data volume is kept.
- GPU tests need the nvidia runtime inside `dind`. They are not covered by this setup.

## The stack

`tests/e2e/support/stack.py` starts the stack described in `tests/e2e/stack/compose.yaml`:

| Part | How it is set up |
|---|---|
| Compose project | Named `cairnml-e2e-<random>` for each session, so two sessions never share containers or networks. |
| Address | Inside `dind`, the stack uses the loopback address and host networking. Services that talk to each other use `127.0.0.1`; the addresses that clients see (the issuer, the public URLs, the storage's public endpoint) come from `CAIRNML_E2E_HOST`, and the listen address from `CAIRNML_E2E_BIND`. |
| Ports | Nine random free ports in 18000 to 18999 (the API, the web app, the sign-in service and its management port, object storage, MongoDB, Redis, and Elasticsearch's HTTP and transport ports). The suite chooses them. Nothing uses the ports 80 or 9000 that `deploy/` uses. |
| State | `/e2e-state/stack` in the isolated environment (the temporary folder on the host run): the data of Keycloak, RustFS, MongoDB, Redis and Elasticsearch, the secrets, and a copy of the realm with this stack's addresses. Removed at the end of a session, unless the stack is reused. |
| Secrets | Generated for the stack (token, session, S3 key). The directory-sync secret is read from the stack's own Keycloak. |
| Accounts | The admin (`e2e-admin`) gets a password generated for the stack. It is a realm user with the `cairnml-admin` role. The plain user of each session is created and removed by the suite. |
| Realm | The committed realm (`deploy/idp/realm-cairnml.json`) read as a template, with its addresses replaced by this stack's. The suite's directory client gets `view-users`, as `bootstrap.sh` grants it. |
| Web app | Its API address is the web server's own `/cairnml/api` route, found from the path it is served under (see Addresses). |

Teardown: `docker compose down --volumes`, then the state folder is removed. The state folder's files were written by
containers as other users, so they are first given back to this user, in a container.

`test_00_isolation.py` checks the claims above at run time: the addresses are not those of the deploy stack, and the API
answering is a container of this session's compose project.

## Addresses

As on a real machine, the web server is the stack's one entrance: the web app is at `/cairnml`, the API at
`/cairnml/api` and the sign-in service at `/cairnml/idp`, all on the web server's port. The suite's `settings.API_URL`
and `settings.WEB_URL` are those addresses. The server names itself after the address the client used (the proxy passes
it in `X-Forwarded-Host`), so no address is configured on it, and the clients (`cairnml init <server>`, `cairnml-agent
init <server>`) take the server's address and work out the rest.

Two services also listen on their own loopback ports, which the suite uses and clients do not: the sign-in service
(`settings.IDP_URL`, for the admin calls made before the web server is up) and the API (which
`test_00_isolation.py` checks).

## Limits

- **Job containers.** In the isolated environment they run on the daemon of `dind`. On the host they run on the host's
  Docker daemon, where the agent stops "orphan" job containers at start-up by label (`cairnml.job`), for any agent on that
  machine. A real agent and the suite's agent on one host would stop each other's orphans, so run the host suite on a
  machine without a live agent of the deploy stack.
- **Agents** are processes with their own `HOME`, device key and worker name; their jobs run in containers.
- **GPU.** The isolated environment has no GPU runtime, so GPU checks are not covered.
- **GitHub tests** need GitHub; they are skipped unless the repository variables are set.

## How it is built

| Part | What it does |
|---|---|
| `docker/run.sh`, `docker/inside.sh` | The host entry point (starts `dind`) and the script that runs inside it: it builds the runner image, then runs `pytest` in a runner container on `dind`'s network, with the suite's environment variables. |
| `docker/compose.yaml`, `docker/runner.Dockerfile`, `requirements.txt` | The `dind` service, the runner image (the test venv, Chromium, the Docker CLI and Compose, git), and the suite's exact pins (`requirements.in` holds the direct ones). |
| `conftest.py` | Starts the stack for the session (autouse), checks that its services answer, resets a reused stack, gives each session its own `HOME`, and enrols the test client through the real sign-in page. Creates the session project and deletes it at the end. Writes the report. |
| `support/stack.py` | Ports, state folder, reuse record and fingerprint, secrets, realm copy, nginx template, compose calls, the identity bootstrap, health waits, teardown. `python -m support.stack --stop` and `--status`. |
| `stack/compose.yaml` | The stack's services and their settings, including the test-only directory sync timer (5 s) and the reset switched on. |
| `stack/prune-networks.sh` | Removes Docker networks that no container uses, after showing them. For the host run, after an interrupted run. |
| `support/settings.py` | Addresses and accounts of the running stack. Set at session start by `configure()`; nothing is read from `deploy/`. |
| `support/cli.py` | Runs the venv's console scripts and the example scripts with the session's `HOME`. Sets `BROWSER` so the sign-in page is opened by the headless browser. |
| `support/browser.py`, `support/web.py` | Fill the sign-in form in headless Chromium, as a person would, and sign in to the web app in a fresh context. |
| `support/keycloak.py` | Creates, disables and removes the test accounts through Keycloak's admin API. |
| `support/api.py` | REST calls as a device: signs the assertion with the device key and exchanges it for a token. |
| `support/client.py` | A person's machine: `client.init()` runs `cairnml init`, and `whoami`, `logout` and the key helpers. |
| `support/agent.py` | A machine that runs jobs: `agent.init()` runs `cairnml-agent init`, and `AgentMachine` runs the managed daemon as a process, with the timers and the shared cache folders of the isolated environment. |
| `support/git.py` | Local git repositories: a bare remote and a checkout of it. |
| `support/workspace.py`, `support/jobs.py` | Running the example scripts and reading their JSON result (the last JSON line); enqueueing and following jobs. |
| `support/report.py` | The report: one row per test, with its description, expectation, outcome and duration. |
| `scripts/` | The example scripts the tests run. Each prints a JSON line at the end. They are written as user code, so they also serve as examples. |

Rules the suite follows:

- **Read back in another process.** A check that a run, artifact, dataset or model is stored reads it in a new process,
  so the result is what the server kept, not what the writer still had in memory.
- **Clean up what it creates.** Everything lives in one project per session. Users and devices are removed by the session
  that made them. The stack itself is removed with its state, unless it is reused.
- **Known defects are failures.** A test that fails because of a defect fails; nothing is marked as expected. Its docstring
  names the defect, and the issue for it is listed below. Nothing is skipped to hide a defect.
- **Skips say why.** The GitHub tests and the one web-address test skip with the reason in the report.
- **Writers may share a process; readers may not.** Several runs or job records can be written by one process, one after
  another. Each check of stored data still reads it in its own process.

## Known defects

No defect is known to fail on the `pre-2.41` trees. The owner-policy test that cairnml-server #3 covered
(`test_05_artifacts.py::test_a_member_who_is_not_the_owner_cannot_delete_the_run`) passed on 2026-11-18, so its row was
removed. Re-check it on the current server before recording it again.

### Not caught by a test


## Status of the last run

Snapshot from 2026-11-18, host run (see "Running on the host"), against the `pre-2.41` trees: server `c1b924ec`, SDK
`12c545cc`, agent `3d20af9a` (v0.0.6), web `128917fd`. It is not a full run of the current tree, and the run was not
complete in one session.

- Full suite, stopped after `test_07_datasets_models.py`'s model tests: `1 failed, 50 passed, 2 skipped, 7 errors in 697.80s`.
  The errors are the dataset module (6) and `test_06_git.py::test_job_clones_the_recorded_commit_and_reports_it`.
- Changed modules, before the shared sign-in fix: `6 failed, 29 passed, 12 errors in 853.56s`.
- Web modules after the fix: `3 failed, 13 passed, 1 error in 262.75s`. Project tabs then passed on its own.
- Queue module (`test_queue_ownership.py`): `16 passed in 128.63s`.

Not run to completion: `test_08` to `test_14`, and `test_access_control.py` (its 2.37 membership checks passed in the
changed-module run). Still failing at the last run:

- `test_07_datasets_models.py` (dataset module): the dataset helper still uses the removed `remote add` flow, which needs a
  GitHub origin and the server's registration. Not rewritten.
- `test_06_git.py::test_repository_on_github_is_refused_at_enqueue_without_the_app`: the enqueue returns `queued 0`
  with exit code 0 instead of a refusal. Real product defect; no issue yet.
- `test_06_git.py::test_job_clones_the_recorded_commit_and_reports_it` and
  `test_12_web_runs.py::test_workers_page_shows_a_running_job_without_a_reload`: the job agent's worker is not listed
  (setup). Probably gianlucapargaetzi/cairnml-agent#113; not confirmed.
- `test_10_web_app.py::test_manage_workers_and_queues_page_opens`: a modal backdrop stays open after sign-in and
  intercepts the dashboard button. Not yet decided whether the overlay is intended.
- `test_12_web_runs.py::test_comparison_view_overlays_both_runs`: `text=loss` not visible in the scalars view within
  10 s. Not analysed beyond the symptom.

## Open results

- **Git job clone** (`test_06_git.py::test_job_clones_the_recorded_commit_and_reports_it`): fixed in the suite. The clone
  runs inside the job container (docker mode maps the cache to `/root/.cairnml` there), which cannot see a local path
  under `/e2e-state`. The test now serves its remote with `git daemon` on the loopback address (`support/git.py`,
  `served`), which the job container reaches with host networking. Passes in a targeted run.
- **Live jobs list** (`test_12_web_runs.py::test_jobs_list_shows_a_running_job_without_a_reload`): fixed in the suite, in
  two parts. The job script now runs from outside the checkout, so the SDK records no SSH repository. And the test opens
  the JOBS tab (`/workers-and-queues/jobs`): it had been left on the dashboard tab, which has no job list. Passes in a
  targeted run.
- **Comparison view**: passed in this run; see Status above.

## Coverage and gaps

[E2E_TESTS.md](E2E_TESTS.md) lists every test. Still not written:

- Job monitoring while a job runs (usage, console growing, status changes).
- Lost-worker detection: a worker that stops reporting is dropped once `default_worker_timeout_sec` (600 s) passes. No test
  checks this, and the e2e stack keeps the production value.
- Resource limits inside a job container, and the job's working folder being removed afterwards.
- Dashboard "Unassigned" bucket and the "All experiments" flat list.
- Contrast checks in light and dark mode (the suite checks the painted background only).

## Speed: what was changed, and why

The suite was made faster in 18 steps. Each one is listed with what it saves. The savings are read from the code's own
ceilings and intervals, not measured; the report now records each test's duration, so the time can be measured.

| # | Change | Saves |
|---|---|---|
| 1 | The stack is reused between runs (`CAIRNML_E2E_REUSE_STACK`). It is rebuilt only when the server or web sources, or the compose file, changed. Starts take a lock. | 3 to 5 min per run after the first |
| 2 | The reused stack is reset at the start of each run (`system.e2e_reset`, off by default), so no test needs a restart for repeatable state. | the restarts repeatability needed |
| 3 | Removed with the scheduler, which had its own sweep for lost workers. A worker is now dropped once its last status report is `default_worker_timeout_sec` old (600 s by default). The suite does not shorten it, and no test checks it. | none |
| 4 | The directory sync runs every 5 s on the e2e stack; the disabled-user test polls every 1 s. | up to about 90 s |
| 5 | The two keyless checks share one daemon. Stopping waits 15 s (was 60 s), registration 60 s (was 240 s). | about 110 s per stopped agent |
| 6 | Fixed sleeps that waited for nothing are gone: the keyless window is 10 s, the queued check polls, the holder's body is 15 s. | about 65 s per run |
| 7 | Polls are 1 s. The deletion test waits on the delete service's own records (`system.e2e_deletions`), then checks the link. | 2 to 9 s per wait |
| 8 | Job waits are 60 s, and a job that ends at once ends the wait. The first (cold) job of a run waits 300 s, the one exception. | about 240 s per job test that never started |
| 9 | UI tests wait for the content they check, with 10 s ceilings, not for `networkidle`. | 30 to 60 s in the web-run module |
| 10 | The API restart test is in its own module (`test_13_api_restart.py`), which runs last. | the modules after it no longer wait |
| 11 | `pytest -n 4 -m "not restart"` runs modules in parallel, with the agent tests in one group. Needs reuse. | the sum of the parallel modules (not measured) |
| 12 | Job records and tracked runs are written in one process per module (`job_script.py --batch`, `track_example.py --batch`). Each job's arguments go to its own task. | about 15 process starts |
| 13 | The artifact upload is 10 MB, still above the SDK's 8 MB multipart threshold. It keeps the `slow` marker. | about half of its transfer |
| 14 | The job script only records the job on the client; the body runs on the agent, with the arguments the task records. Found while implementing. | up to 600 s per abort test |
| 15 | The suite runs in an isolated environment: one `dind` container, with the runner, the stack and the job containers inside it. | no host daemon in use |
| 16 | Removed: a no-op `_unregister` stub in the agent, which a later definition in the same class overrode. | none |
| 17 | A script's result is its last JSON line (the SDK can print a GPU notice after it), and the runner has pandas, numpy and pillow. | the tracking and artifact tests run |
| 18 | Removed with the scheduler: the SDK checks the server's 2.4 enqueue level, as upstream does. | none |

## Open work

- **A job that never starts fails with its reason.** Done in `support/jobs.py`: a wait fails at once when the job ends in
  another status, or when its console shows the container cannot reach the server before the body starts. Every failure
  names the status, the agent's reason and the last console line. The unreachable path is checked with a fake API only;
  no test yet produces an unreachable server.
- **Long jobs and the job token.** Tracked in cairnml-agent issue #3, including the tests the renewal needs.
- **The two open results above**, both suite fixes (see Open results).

## Clean-up of a real stack

The suite does not touch the stack under `deploy/`. Earlier runs, made before the isolated stack existed, left records
there, and they have been removed: the probe project (through the API), 16 workers (directly in the database), four `no-project` tasks (through the API), and seven `e2e-member-*` user records (directly in the
database). The tracking test now deletes its own task.
