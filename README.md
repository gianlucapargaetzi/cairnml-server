# CairnML

Our experiment tracking, remote jobs and dataset setup, on a modified fork of [ClearML](https://clear.ml). CairnML is not ClearML and is not endorsed by ClearML, Inc.; see [DISCLAIMER.md](DISCLAIMER.md), [NOTICE](NOTICE) and [LICENSES.md](LICENSES.md).

- **Track** a training script with a few lines of Python: metrics, parameters, logs and artifacts are recorded on our server.
- **Explore** runs in the web app.
- **Run** jobs on agents that we operate. A job waits in a queue until an agent that listens to that queue takes it.
- **Version** datasets with git and DVC. A dataset is a git repository, its commit is its version, and every run records the commit it used.
- **Sign in** with our identity provider (Keycloak). Each machine has its own key; nobody copies access keys.

This repository is the CairnML server, with the setup that runs it: the sign-in service, the scripts to start them, and the end-to-end suite. The SDK, the agent and the web app are fetched by a script, each at the commit that its pin file names (below).

## What is where

| Path | What it is |
|---|---|
| `apiserver/`, `docker/` | The CairnML server, and the image it builds |
| `deploy/compose.yaml` | The whole stack: Keycloak (sign-in), and the CairnML server built from this repository |
| `deploy/idp/realm-cairnml.json` | The realm, with the CairnML clients |
| `deploy/scripts/bootstrap.sh` | Sets up the stack on a host |
| `deploy/scripts/teardown.sh` | Removes the stack and this machine's keys (`--dry-run` first) |
| `deploy/scripts/check-cairnml-secrets.sh` | Fails if a secret is committed to a CairnML repository |
| `scripts/fetch-sources.sh` | Puts the SDK, the agent and the web app in their folders, each at the commit its file in `docker/build/` names |
| `docker/build/sdk-ref`, `agent-ref`, `web-ref` | The pins: the commit of each repository that this one builds from |
| `cairnml/`, `cairnml-agent/`, `web/` (not in git) | The SDK, the agent and the web app, made by `scripts/fetch-sources.sh` |
| `cairnml/docs/datasets.md` | How datasets work: `cairnml-dataset`, `Dataset.get`, outside datasets, the cache |
| `external-datasets` (its own repository, not a clone here) | One pin file and one adapter per outside dataset (Hugging Face, git, local folders), with the tests that keep its `main` working |

## Requirements

On the server host: Linux (`bootstrap.sh` uses `getent`), Docker Engine with Compose v2.20 or newer, `git`, `openssl`, `python3` and `curl`.

On the machines of users and agents: Python 3 and `git`; the SDK and agent install from PyPI (`pip install cairnml`, `pip install cairnml-agent`). Agents also need Docker for jobs that run in containers.

`docker/build/sdk-ref`, `agent-ref` and `web-ref` each name a commit of one repository, and `scripts/fetch-sources.sh` clones those commits into `cairnml/`, `cairnml-agent/` and `web/`. Clone this repository, then run the script:

```bash
git clone git@github.com:gianlucapargaetzi/cairnml-server.git
cd cairnml-server
scripts/fetch-sources.sh
```

The repositories are public (`gianlucapargaetzi/cairnml-server`, `cairnml`, `cairnml-agent` and `cairnml-web`), so no special access is needed.

The forks do not share history with ClearML on GitHub. To compare with ClearML, add it as a remote in a fork: `git remote add upstream https://github.com/clearml/<name>.git`.

## 1. Server

### Host names

The sign-in service and the server are reached by name, and the name must be the same on every machine. Add this line to `/etc/hosts` on the server host, and on every other machine that uses the server, with the server's address in place of `127.0.0.1` on the other machines:

```
127.0.0.1 cairnml-idp cairnml
```

`bootstrap.sh` refuses to run until both names resolve.

### Start

```bash
cd deploy
./scripts/bootstrap.sh
```

The first run takes a while: it builds the CairnML images from this repository and the web app. It does the following, and prints nothing secret:

1. Starts Keycloak with the `cairnml` realm, and sets the admin login from `deploy/.env` (created on the first run with a generated password).
2. Writes the CairnML secrets to `$CAIRNML_HOME/secrets/cairnml/` (once; they are kept on later runs): the token and session keys. If `$CAIRNML_HOME/secrets/github-app.pem` exists, it is copied there as the GitHub key.
3. Creates the three CairnML clients in Keycloak (`cairnml-web`, `cairnml-sdk`, `cairnml-server`) and updates them to `deploy/idp/realm-cairnml.json` on every run. It stores the secret of `cairnml-server` (used to read users from Keycloak) in `deploy/secrets/cairnml/idp_directory_secret`.
4. Starts the CairnML server (`deploy/compose.yaml`).

Check that the server answers:

```bash
curl -X POST http://cairnml:8008/v2.8/login.supported_modes -H 'Content-Type: application/json' -d '{}'
```

### First sign-in

Open `http://cairnml:8080`. You are sent to the sign-in page of Keycloak. Sign in as the admin user named by `KC_ADMIN` in `deploy/.env`, with the password from the same file. Your CairnML account is created at that first sign-in.

Admins are the users with the `cairnml-admin` role in Keycloak. Give other people the role, or leave them as plain users. Keycloak's admin console is at `http://cairnml-idp:8180`.

### Optional: GitHub

Jobs that clone a GitHub repository need a GitHub App. Put its private key in `$CAIRNML_HOME/secrets/github-app.pem`, set `GITHUB_APP_ID` in `deploy/.env`, and run `./scripts/bootstrap.sh` again. Then restart the API server:

```bash
docker compose -f deploy/compose.yaml up -d --force-recreate apiserver
```

Without the App, jobs that clone a repository are refused when they are enqueued.

Jobs that use a dataset clone its repository too. The dataset registry (the `external-datasets` repository) is the only list of dataset repositories: a job gets a read-only token for the registry and for the repositories its entries point to, and for nothing else. Name the registry and the owners whose datasets may be registered in `deploy/.env`, install the App on the registry and on the dataset repositories, and restart the API server as above:

```bash
CAIRNML_DATASET_REGISTRY=acme/external-datasets
CAIRNML_DATASET_OWNERS='["acme"]'
```

A dataset enters the registry with `cairnml-dataset register`; no server setting changes. The server reads the registry's default branch, so a new entry works about a minute after its pull request merges. A person uses their own git login.

### Addresses and https

The addresses are set in `deploy/compose.yaml` (`CAIRNML__apiserver__idp__*`): the web app at `http://cairnml:8080`, the API at `http://cairnml:8008`. To serve over https, change those addresses to https, and set `CAIRNML_COOKIE_SECURE=true` in `deploy/.env`. The server refuses to start with https addresses and an insecure session cookie.

### Keep the data

The stack's state is in one folder, `CAIRNML_HOME` (default `~/.local/share/cairnml`, outside the repository): `data/` holds the users, databases, logs and object storage, and `secrets/` holds the keys. Set `CAIRNML_HOME` in `deploy/.env` to move it. Back up that folder; losing `secrets/cairnml/token_secret` signs everyone out, and losing the database loses the runs.

The GitHub App key (`secrets/github-app.pem`) cannot be created again, so no teardown mode removes it. `deploy/scripts/teardown.sh` removes the containers and images, and with `--data` the data. It never removes the secrets, `deploy/.env` or the GitHub App key. Run it with `--dry-run` first.

### Object storage and the NAS

Object storage (RustFS, with the buckets `dataset`, `model`, `artifact` and `debug`) runs in this stack, on the server host, and keeps its data in `$CAIRNML_HOME/data/rustfs`. Artifacts, models and debug data move through presigned URLs. The files of datasets move through DVC, with short-lived keys that the server issues for the `dataset` bucket only (`datachunks.dataset_credentials`), so no access key is stored on a machine. Run `deploy/scripts/bootstrap.sh` again after an update: it gives the server's storage user the policy for issuing those keys. The architecture diagram (`docs/architecture/`) shows it on a separate NAS, which is not set up yet. To move it, change the `rustfs` volume in `deploy/compose.yaml` to the NAS path, and set `CAIRNML_STORAGE_PUBLIC_URL` in `deploy/.env` to an address that clients can reach.

## 2. Client (a person's machine)

Install the SDK from PyPI, in a virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install cairnml
```

To work on the SDK itself, install the fork from this repository instead: `pip install -e ./cairnml`.

Sign in and enrol the machine once, naming the server on the command line. Your browser opens the sign-in page, and the sign-in always asks for your credentials:

```bash
cairnml-init --server http://cairnml:8008
cairnml-whoami
```

`cairnml-whoami` shows the user and the device this machine signs in as. It uses the device key, and needs no browser. The key is in `~/.cairnml/device.key` (readable by you only); the configuration is `~/cairnml.conf`. No access key is copied.

To enrol again, for example as another person, use `--force`. The previous device is revoked first:

```bash
cairnml-init --server http://cairnml:8008 --force
```

To sign this machine out, revoke it and remove its key:

```bash
cairnml-logout
```

If the device cannot be revoked (for example, the server is unreachable), `cairnml-logout` removes nothing, and you can run it again later.

The machine must be able to resolve `cairnml` and `cairnml-idp` to the server, as in "Host names" above.

Check it with a short run:

```python
from cairnml import Task

task = Task.init(project_name="demo", task_name="first run")
logger = task.get_logger()
for step in range(10):
    logger.report_scalar(title="loss", series="train", value=1.0 / (step + 1), iteration=step)
task.close()
```

The run appears in the web app under the project `demo`, with its metrics.

### Datasets

A dataset is a git repository with a `data/` folder. Git holds the history, DVC holds the files in the `dataset` bucket, and the commit is the version. Install the extra (Python 3.9 or newer, and `git`):

```bash
pip install "cairnml[dataset]"
```

Create an empty repository on GitHub, clone it, and inside it:

```bash
cairnml-dataset init                  # git tracking, file storage, dataset.yaml
cp -r /path/to/measurements data/     # your files
cairnml-dataset status                # branch, and what is new, changed or deleted
cairnml-dataset add                   # records data/ as it is now, deleted files included
cairnml-dataset commit -m "March measurements"   # checks the data and commits it with its footprint
cairnml-dataset push                  # the files first, then the commit (through a pull request)
cairnml-dataset register              # once: adds the dataset to the registry
```

A dataset must be readable in the standard format: `dataset.yaml` says `format: native` (the standard sample table is in `data/`) or names an adapter (`cairnml-dataset init --adapter <name>`). `commit` refuses data that is neither, and writes `dataset.lock.json`, a checksum record that a GitHub Action in the dataset repository verifies from git alone. See `cairnml/docs/datasets.md`.

A script uses a version by its commit. The run records the repository, the commit and a hash of the files, and the files are kept in a per-user cache, so a second run does not download them again:

```python
from cairnml.datasets import Dataset

data = Dataset.get("https://github.com/acme/cars-data.git", "3f2a9c1")   # a commit, not a branch, to rerun it later
print(data.path)
```

Outside datasets (Hugging Face, git, a local folder) are read through adapters in the `external-datasets` repository, into one standard shape of samples with named modalities and typed labels:

```python
data = Dataset.get_external("coco-det")            # the latest main of external-datasets
data = Dataset.get_external("coco-det", "a1b2c3d") # a fixed commit, to rerun an earlier run exactly
```

The cache is `~/.cairnml/cache/datasets`, limited to 50 GB by default (`cairnml-dataset cache info`, `cache limit 20GB`, `cache clean`). See `cairnml/docs/datasets.md` for the details, including how to write an adapter.

To enrol a second laptop, run `cairnml-init --server ...` on it. Each machine has its own key. A lost machine is revoked by its owner or an admin with the `devices.revoke` call of the API, and its key stops working at once. There is no page for this in the web app yet.

## 3. Agent (a machine that runs jobs)

An agent is enrolled by an admin, because it runs other people's code.

Install the agent from PyPI, in its own virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install cairnml-agent
```

To work on the agent itself, install the fork from this repository instead: `pip install -e ./cairnml-agent`.

Enrol it. An admin signs in when the browser opens; a non-admin is refused, and no configuration is written:

```bash
cairnml-agent init
```

Then start the agent on a queue. It takes one job at a time from the queue and runs it:

```bash
cairnml-agent daemon --queue default
```

Add `--docker` to run each job in a container, and `--gpus 0` to give the agent chosen GPUs. A machine with several GPUs can run one agent per GPU, each on its own queue. Run `cairnml-agent daemon --help` for the rest, including services mode.

Each job runs with its own token, issued to this agent when the job starts. The agent renews it while the job runs, and the job never gets the agent's own token. The repository is cloned with a short-lived GitHub token for that one repository.

### In a container

The agent can also run as a container, in `deploy/agent/`. The agent container runs on the host's Docker daemon; it starts each job's container on a second daemon, in a privileged `dind` container. The host's daemon never runs a job and is not reachable from the jobs. Start it from `deploy/agent/`:

```bash
docker compose build agent
docker compose up -d dind
docker compose run --rm agent cairnml-agent init
docker compose up -d agent
```

The server address in the agent's configuration must be reachable from inside `dind`: a LAN name or IP, not `localhost`. Only one agent may run per daemon. See `deploy/agent/README.md` for the layout, the volumes and the limits.

### Queues

A job waits in a queue until an agent that listens to it takes the job. The `default` queue exists from the start; create more in the web app (Workers & Queues), for example one per kind of machine.

Put a task in a queue from a script, or from the web app (enqueue on the task's page):

```python
from cairnml import Task

task = Task.init(project_name="demo", task_name="training")
Task.enqueue(task, queue_name="default")       # run the recorded task on an agent
# or: task.execute_remotely(queue_name="default")   # stop this process and run the script on an agent
```

An agent takes one job at a time, so a job waits while its agent is busy. A machine that should run several jobs at once runs several agents, each on its own queue or GPU. The Workers page shows each machine, the task it runs and its live usage, and the Queues page shows what waits. A job that is waiting but never starts has no agent listening to its queue.

## 4. Web app

Open `http://cairnml:8080`. To run the web app from its source for development, see `web/docs/dev-proxy.md`.

The dashboard lists the projects at the top and the recent experiments at the bottom. Double-click a project to open its dashboard: the project's tasks as folders at the top, and its recent experiments at the bottom. Double-click a task folder to list its experiments, numbered in the order they were created. "All experiments" lists every experiment in the project.

### Terms

- **Task**: a procedure, such as "train model X". Its runs are experiments, and they are grouped under it by name.
- **Experiment**: one execution of a task. Each run is an experiment, whether it was started from the web app, with `execute_remotely()` or `enqueue()`, or run locally with `Task.init`. Each experiment has its own id, metrics and artifacts.
- **Job**: one experiment taken from a queue by an agent, from the moment the agent starts it until it ends.

In the SDK and the API, the record of an experiment is still called a `Task`.

## 5. Checks

- `deploy/scripts/check-cairnml-secrets.sh` fails if a secret, a private key or an `.env` file is committed to one of the CairnML repositories.

## 6. Troubleshooting

| Symptom | What to do |
|---|---|
| `bootstrap.sh` asks you to add a host line | Add the line from "Host names", then run it again |
| The server does not answer after bootstrap | Elasticsearch takes a minute to start. Check `docker compose -f deploy/compose.yaml logs apiserver` |
| Sign-in loops, or the web app shows a sign-in error | The addresses in `deploy/compose.yaml` must match the address you use in the browser |
| `cairnml-init` says the machine is enrolled | The key file `~/.cairnml/device.key` exists. Remove it only to enrol again, and revoke the old device in the web app |
| `cairnml-agent init` is refused | Only an admin may enrol an agent. Ask an admin to run it |
| A job is refused when it is enqueued | The repository's commit is not on GitHub, or the GitHub App is not installed on it. The message says which |
| A job stays queued | No agent listens to its queue. Start `cairnml-agent daemon --queue <name>`, and look for the worker on the Workers page |
| `Dataset.get` says a commit is not on the repository, or its files are not in storage | Push it: `cairnml-dataset push`. A commit that exists only on one machine cannot be used by another |
| `cairnml-dataset commit` says the data is not native and names no adapter | `dataset.yaml` must say `format: native` (with `samples.parquet` and `manifest.json` in `data/`) or `adapter: <name>`. Run `cairnml-dataset check` to see what is wrong |
| `push` says the footprint does not hold | The data or `dataset.yaml` changed after the check. Run `cairnml-dataset commit` again |
| An adapter stops a run with a message that names it | The source changed its layout. Add an adapter for the new revisions and list them under `overrides` in the dataset's pin file in `external-datasets` |

## 7. Status

The setup is complete. Not yet verified end to end on a live stack: the browser sign-in for each kind of machine, and a job from enqueue through to its artifacts.

## 8. Tests

Two kinds: unit tests in each fork, and the end-to-end suite in `tests/e2e/`, which starts its own stack and checks the
whole workflow against it (see [docs/e2e_testing/E2E.md](docs/e2e_testing/E2E.md) and [docs/e2e_testing/E2E_TESTS.md](docs/e2e_testing/E2E_TESTS.md)).

### Unit tests

| Fork | Command | Notes |
|---|---|---|
| `cairnml` (SDK) | `cd cairnml && python -m pytest tests` | Runs without services. The dataset tests start git and need `pyarrow` (the dataset extra); the run takes a minute or two. |
| `cairnml-agent` | `cd cairnml-agent && python -m pytest tests --ignore=tests/requirements` | Needs the fork installed (`pip install -e .`). `tests/package` needs a running server and is left out. |
| `cairnml-server` (this repository) | `python -m pytest -m "not integration" apiserver/tests` | Needs Redis (`CAIRNML_REDIS_SERVICE_HOST`, `CAIRNML_REDIS_SERVICE_PORT`) and the packages in `apiserver/requirements.lock`. Tests marked `integration` need MongoDB and Elasticsearch or a running API server, and run in the end-to-end suite. |
| `cairnml-web` (`web/`) | `cd web && npm ci && npm test` | Karma, which needs Chrome. Not run in the last check. |

The server's `integration` tests (MongoDB, Elasticsearch or a running API server) run in the end-to-end stack's compose project, whose services are on a private network. CI runs the unit tests of each repository on each pull request.

### Working on the SDK and the agent, not the release

`scripts/dev-env.sh` creates `.venv` with the SDK and the agent installed editable from their folders, and the server's other requirements. Every test and script then runs the working tree, from the root or from any folder. The SDK's dataset extra is `dvc` and `pyarrow`; `dev-env.sh` installs `pyarrow` only, because `dvc` needs `dpath` 2 and the server pins `dpath` below 2.

`scripts/build-dev-wheels.sh` builds the SDK and the agent from the working trees into `dist/dev`, versioned `<version>+dev.<commit>` (with `.dirty` when a tree has uncommitted changes). Another machine or agent installs them with `pip install --find-links dist/dev cairnml cairnml-agent`. They are local builds and are never uploaded.

The stack builds its server image from these source trees: `CAIRNML_FROM=source` in `deploy/compose.yaml` and in the end-to-end stack. The Docker Hub image is built from `requirements.lock` (`CAIRNML_FROM=pypi`).

### Release order

A change to the SDK or the agent reaches the server image in this order: release the SDK and the agent (bump `version.py`, tag `vX.Y.Z`; the publish workflow puts the package on PyPI), regenerate `apiserver/requirements.lock` for the new versions, then tag the server (the Docker Hub image is built from the lock).

### End-to-end tests

Prerequisites, and the full list of what the suite checks, are in [docs/e2e_testing/E2E.md](docs/e2e_testing/E2E.md). In short:

```bash
uv venv .venv --python 3.12
uv pip install -r tests/e2e/requirements.txt
uv pip install -e ./cairnml -e ./cairnml-agent
playwright install chromium

.venv/bin/pytest tests/e2e                 # everything: starts its own stack, an hour or more
.venv/bin/pytest tests/e2e -m "not slow"   # without the long checks
.venv/bin/pytest tests/e2e -k artifacts    # one area
```

The suite does not use `deploy/`. It needs Docker, nine free ports in 18000 to 18999 on the loopback interface, the SDK, the agent and the web app in their folders (`scripts/fetch-sources.sh`). Known failures are listed in [docs/e2e_testing/E2E_TESTS.md](docs/e2e_testing/E2E_TESTS.md) and as issues in each repository.
