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
  paragraph of its docstring; a test without one is listed as not written. The runner writes the folder as root, so it
  is root-owned on the host.
- **Markers** (`tests/e2e/pytest.ini`): `agent`, `docker`, `ui`, `github`, `slow`, `restart`, `xdist_group`.
- **GitHub tests.** `CAIRNML_E2E_GITHUB_REPO=owner/name` names a repository on github.com that the GitHub App can read;
  `CAIRNML_E2E_GITHUB_PRIVATE_REPO=owner/name` names one it cannot. Without them, the `github` tests are skipped with that
  reason.

`system.e2e_reset` removes the projects, tasks and scheduler workers whose name starts with the suite's prefix (`e2e`),
with their contents. It is off unless the server's `apiserver.e2e.reset_enabled` is set, which only the suite's compose
file does, and it requires an admin.

### Running on the host (not the isolated environment)

`.venv/bin/pytest tests/e2e` still runs the suite on the host's Docker daemon, with the stack on the host's loopback
interface. It needs Docker with Compose v2, the SDK and the agent as submodules, the web app in `web/` (`scripts/fetch-web.sh`), Python 3.12 with the suite's requirements and the
SDK and the agent (`uv pip install -r tests/e2e/requirements.txt`, then `uv pip install -e ./cairnml -e ./cairnml-agent`),
Chromium for Playwright, and nine free ports in 18000 to 18999. It has not been run since the isolated environment was
built, and the isolated environment is the one to use. Its job containers run on the host's daemon (see Limits).

## The isolated environment

The host runs one container, `dind`: a Docker daemon of its own. Everything the suite uses runs inside it: the test
runner, the CairnML stack, the managed agent, and the agent's job containers. The host's Docker daemon starts `dind` and
nothing else, and nothing inside it is published to the host.

| Where | What |
|---|---|
| host | `docker compose -f tests/e2e/docker/compose.yaml up dind`, then `inside.sh` runs in it. Nothing else. |
| `dind` (privileged) | Its daemon runs every container below. Its filesystem holds `/workspace` (the repository, mounted from the host), `/e2e-state` and `/e2e-cache` (volumes), and the images it pulls and builds. |
| runner (in `dind`) | Built by `inside.sh` from `runner.Dockerfile`: the test venv (the suite, the SDK and the agent from the submodules, editable), Chromium, the Docker CLI and Compose, git. It runs `pytest`, the managed agent and the browser. |
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
| Ports | Nine random free ports in 18000 to 18999 (the API, the web app, the sign-in service and its management port, object storage, MongoDB, Redis, and Elasticsearch's HTTP and transport ports). The suite chooses them. Nothing uses the ports 8008, 8080, 8180 or 9000 that `deploy/` uses. |
| State | `/e2e-state/stack` in the isolated environment (the temporary folder on the host run): the data of Keycloak, RustFS, MongoDB, Redis and Elasticsearch, the secrets, and a copy of the realm with this stack's addresses. Removed at the end of a session, unless the stack is reused. |
| Secrets | Generated for the stack (token, session, S3 key). The directory-sync secret is read from the stack's own Keycloak. |
| Accounts | The admin (`e2e-admin`) gets a password generated for the stack. It is a realm user with the `cairnml-admin` role. The plain user of each session is created and removed by the suite. |
| Realm | The committed realm (`deploy/idp/realm-cairnml.json`) read as a template, with its addresses replaced by this stack's. The suite's directory client gets `view-users`, as `bootstrap.sh` grants it. |
| Web app | Its API address is the web server's own `/api` route, set with `configuration.json` (see Addresses). |

Teardown: `docker compose down --volumes`, then the state folder is removed. The state folder's files were written by
containers as other users, so they are first given back to this user, in a container.

`test_00_isolation.py` checks the claims above at run time: the addresses are not those of the deploy stack, and the API
answering is a container of this session's compose project.

## Addresses

The clients (the SDK, the CLI, the agent) take the API address as a command-line argument or from the stack's identity
settings, so nothing depends on a fixed address. Two things in this build assume the default ports, and the suite works
around them:

- The web app takes its API address from its own page address with port `8008` (`cairnml-web`, `guessAPIServerURL`).
  The suite writes `{"apiBaseUrl": "/api"}` to the web server's `configuration.json`, which the image reads from
  `/mnt/external_files/configs/`. Filed as `cairnml-web` issue #3.
- `cairnml-init --server <web address>` derives the API address from the web address for ports 8080 and 8008 only. The
  test for that is skipped on other ports (`test_02_enrolment.py::test_init_accepts_the_web_address_and_finds_the_api`).

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
| `docker/compose.yaml`, `docker/runner.Dockerfile`, `docker/requirements.txt` | The `dind` service, the runner image (the test venv, Chromium, the Docker CLI and Compose, git), and the runner's packages. |
| `conftest.py` | Starts the stack for the session (autouse), checks that its services answer, resets a reused stack, gives each session its own `HOME`, and enrols the test client through the real sign-in page. Creates the session project and deletes it at the end. Writes the report. |
| `support/stack.py` | Ports, state folder, reuse record and fingerprint, secrets, realm copy, nginx template, compose calls, the identity bootstrap, health waits, teardown. `python -m support.stack --stop` and `--status`. |
| `stack/compose.yaml` | The stack's services and their settings, including the test-only timers (2 s scheduler sync, 1 s sweep, 5 s directory sync) and the reset switched on. |
| `stack/prune-networks.sh` | Removes Docker networks that no container uses, after showing them. For the host run, after an interrupted run. |
| `support/settings.py` | Addresses and accounts of the running stack. Set at session start by `configure()`; nothing is read from `deploy/`. |
| `support/cli.py` | Runs the venv's console scripts and the example scripts with the session's `HOME`. Sets `BROWSER` so the sign-in page is opened by the headless browser. |
| `support/browser.py`, `support/web.py` | Fill the sign-in form in headless Chromium, as a person would, and sign in to the web app in a fresh context. |
| `support/keycloak.py` | Creates, disables and removes the test accounts through Keycloak's admin API. |
| `support/api.py` | REST calls as a device: signs the assertion with the device key and exchanges it for a token. |
| `support/client.py` | A person's machine: `client.init()` runs `cairnml-init`, and `whoami`, `logout` and the key helpers. |
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

Each defect has an issue in the repository where the fix belongs. The tests named here are the ones that fail, or passed
after the fix, because of it.

| # | Issue | What happens | Tests |
|---|---|---|---|
| Policy | [cairnml-server #3](https://github.com/gianlucapargaetzi/cairnml-server/issues/3) | A plain user can delete another user's run. Expected 403 until user access management exists (decided). | `test_05_artifacts.py::test_a_member_who_is_not_the_owner_cannot_delete_the_run` |

Defects that were fixed are not listed. `Task.set_parameters` replaces the whole parameter set, as its docstring says; the
example script writes the group first, and `test_set_parameters_replaces_the_whole_set_and_set_parameter_adds` documents it.

### Not caught by a test

- **Fixed-port derivation in `cairnml-init`.** Skipped on other ports, so it is not run here.

## Status of the last full run

This section is a snapshot from one run. It is not kept up to date: the unit tests run on every pull request, and the full suite is run by hand before a release.

The dataset tests in `test_07_datasets_models.py` were replaced after this run, when datasets moved to git and DVC, and have not been run yet. The numbers below are from before the change.

Latest full run, in the isolated environment, from a stack that was stopped first, with no restarts: 101 tests,
16 minutes 21 seconds, **92 passed, 6 failed, 3 skipped**.

The 6 that failed, by cause:

- **Known defects, failing as designed:**
  - `test_a_member_who_is_not_the_owner_cannot_delete_the_run`: the owner policy is not built (cairnml-server #3).
- **Open, explained (see Open results):** the git job clone, and the live jobs list.

The 3 skipped: `test_init_accepts_the_web_address_and_finds_the_api` (the suite runs on ports other than the defaults), and
the two GitHub tests (no GitHub repository is configured).

Fixed since the earlier runs: the job completion (the agent's `_unregister` fix), the
tracking and artifact tests (the result parser, and the runner's data packages), and the comparison view (it passes in
this run; it failed in an earlier one, and it passes alone too, so its failure was timing or order, not the view).

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
| 3 | A lost worker is found by the scheduler's own sweep (`sweep_interval_sec`), not by a placement pass. The e2e stack syncs every 2 s and sweeps every 1 s. | about 55 s per lost-worker test |
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
| 14 | The job script only records the job on the client; the body runs on the agent, with the arguments the task records. Found while implementing. | up to 600 s per abort test, 300 s for the lost-agent test |
| 15 | The suite runs in an isolated environment: one `dind` container, with the runner, the stack and the job containers inside it. | no host daemon in use |
| 16 | The agent's `_unregister` is a no-op. Its callers had outlived the method, so a finished job raised after its status was reported. | job completion is reported |
| 17 | A script's result is its last JSON line (the SDK can print a GPU notice after it), and the runner has pandas, numpy and pillow. | the tracking and artifact tests run |
| 18 | `Task.enqueue` checks the server's API level, not the one in use (SDK fix). | enqueue after a service call works |

## Open work

- **A job that never starts fails with its reason.** Done in `support/jobs.py`: a wait fails at once when the job ends in
  another status, or when its console shows the container cannot reach the server before the body starts. Every failure
  names the status, the agent's reason and the last console line. The unreachable path is checked with a fake API only;
  no test yet produces an unreachable server.
- **Long jobs and the job token.** Tracked in cairnml-agent issue #3, including the tests the renewal needs.
- **The two open results above**, both suite fixes (see Open results).

## Clean-up of a real stack

The suite does not touch the stack under `deploy/`. Earlier runs, made before the isolated stack existed, left records
there, and they have been removed: the probe project (through the API), 16 workers with their scheduler records (directly
in the database), four `no-project` tasks (through the API), and seven `e2e-member-*` user records (directly in the
database). The tracking test now deletes its own task.
