# Troubleshooting

Find the symptom you see. Each entry gives the usual causes in the order to check them. The commands are run in `deploy/`
on the server's machine, and the stack's state is as described in [Operate the server](operate.md).

To see what is running and which part is unhealthy:

```bash
cd deploy
docker compose ps
docker compose logs --tail 100 <service>      # apiserver, webserver, cairnml-idp, rustfs, rustfs-init, mongo, redis, elasticsearch, async-delete
```

- [The server does not start, or the setup fails](#the-server-does-not-start-or-the-setup-fails)
- [Sign-in fails](#sign-in-fails)
- [A machine cannot enrol](#a-machine-cannot-enrol)
- [An agent does not show on the Workers page](#an-agent-does-not-show-on-the-workers-page)
- [A task stays queued](#a-task-stays-queued)
- [An upload fails](#an-upload-fails)
- [Object storage is unreachable](#object-storage-is-unreachable)
- [A job from GitHub is refused](#a-job-from-github-is-refused)
- [Dataset registration is refused](#dataset-registration-is-refused)

## The server does not start, or the setup fails

The setup (`deploy/scripts/bootstrap.sh`) stops with a message on its own line. The common ones:

| Message | Cause and fix |
|---|---|
| `docker is required`, `docker compose v2.20+ is required`, `openssl is required`, `python3 is required`, `curl is required` | Install the missing tool, then run the setup again. |
| `set KC_ADMIN in deploy/.env to a name other than kcadmin` | `kcadmin` is the temporary admin that Keycloak creates. Choose another `KC_ADMIN`. |
| `could not sign in to Keycloak: check KC_ADMIN and KC_ADMIN_PASSWORD` | The sign-in service did not accept the admin login within three minutes. Check the `cairnml-idp` logs. |
| `the new admin cannot sign in: the temporary admin is kept` | The admin could not sign in with the new password, so the setup kept the temporary admin. Check `KC_ADMIN` and `KC_ADMIN_PASSWORD`, then run the setup again. |
| `keycloak did not become ready` | The sign-in service did not start. Check `docker compose logs cairnml-idp`. |
| `could not read the cairnml-server client secret from Keycloak` | The realm's `cairnml-server` client was not found. Check the `cairnml-idp` logs, then run the setup again. |

If the setup ends without `CairnML server is up`, the server did not answer within three minutes. Check its state with
`docker compose ps`: `apiserver` should be `healthy`.

- **`rustfs-init` exited with an error.** The server waits for it to finish successfully. See [Object storage is unreachable](#object-storage-is-unreachable).
- **`apiserver` restarts, and its log says `failed connecting to ElasticSearch service`.** Elasticsearch was not ready when the server started. Check `docker compose ps elasticsearch` and its logs; once it is healthy, start the server with `docker compose restart apiserver`.
- **A Redis or MongoDB connection error in the `apiserver` log.** The server needs both at start-up. Check `docker compose ps mongo redis`.
- **A port is already in use.** Another program holds port 80, 9000 or 9001. Change the port in `deploy/.env` (see [Ports](operate.md#ports-and-the-firewall)) and run the setup again.

## Sign-in fails

**The browser shows "sign-in refused" after the sign-in page.** The server refused the sign-in and records the refusal as
an audit event. The causes are:

- The sign-in was already used. Start again from the web app's address.
- The user is disabled in the `cairnml` realm. Enable the user in the Keycloak admin console (see [Disable or remove a user](../../README.md#disable-or-remove-a-user) in the README).
- The sign-in did not start and end at the same address. The server names itself after the address the client used, so start and finish at one address, and not at a second name for the same machine.

**The admin cannot sign in.** Use the user named in `KC_ADMIN` and the password in `deploy/.env`. If you changed
`KC_ADMIN_PASSWORD`, run `deploy/scripts/bootstrap.sh`: the setup signs in with the old password and then sets the new one.

**A user signs in but cannot do admin tasks.** Only users with the `cairnml-admin` realm role are admins. Assign the role
(see the README, section "Users and admins").

## A machine cannot enrol

Enrolment is what `cairnml init` (SDK) and `cairnml-agent init` do. The server refuses it with a message that names the reason:

| Message | Cause and fix |
|---|---|
| `only an admin may enrol an agent` | Only an admin can enrol an agent. Sign in as an admin, or ask one to enrol it. |
| `user is disabled` | The user who enrols is disabled in the `cairnml` realm. |
| `unknown device`, `device revoked` | The machine's key was revoked, or the record was removed. Enrol the machine again. |
| `unknown owner`, `owner disabled` | The user who owns the device is missing or disabled. Enable the user, or enrol the machine as an enabled user. |
| `malformed assertion`, `invalid assertion` | The machine's signature or clock is wrong. Check the machine's date and time, then try again. |
| `assertion already used` | The same signed request was sent twice. Run the command again. |

On a machine without a browser, `cairnml init` needs the device sign-in. On an installation made before that sign-in was
added, it has to be turned on once for the `cairnml-sdk` client (see the README, section "Machines without a browser").

## An agent does not show on the Workers page

Work through these in order:

1. **The agent is running** on its machine, with the server's address it was enrolled with.
2. **The agent can reach the server** at `http://<server-ip>/cairnml/api` (or the port set by `CAIRNML_HTTP_PORT`). Check the port from the agent's machine, for example with `curl -fsS http://<server-ip>/cairnml/api/v2.8/login.supported_modes -H 'Content-Type: application/json' -d '{}'`. It should answer with JSON.
3. **The agent is enrolled**, and it was enrolled by an admin. See [A machine cannot enrol](#a-machine-cannot-enrol).
4. **The agent reports to a live server.** A worker that has not reported for `services.workers.default_worker_timeout_sec` (600 seconds by default) is no longer listed. Restart the agent, and it appears at its next report.

## A task stays queued

A queued task waits until an agent that watches its queue takes it.

- **No agent watches the queue.** The queue shows no worker in the web app, which is the case of the "no workers assigned to queue" dialog. Start an agent for that queue; see the agent's README for how it chooses its queues.
- **The agent is running, but it is not on the queue of the task.** Check the queue name the agent was started with, against the queue the task was sent to.
- **The agent cannot take the job.** Check the agent's log on its machine. A job from a GitHub repository whose commit or App is missing is refused when it is queued, and does not stay queued (see [A job from GitHub is refused](#a-job-from-github-is-refused)).

## An upload fails

Uploads and downloads go straight from the client to object storage, through presigned addresses. The server names the
address in each one: the address the client used for the server, on `CAIRNML_STORAGE_PORT`. So a client needs to reach
that port too.

1. **The port is reachable.** From the client's machine, `curl -fsS http://<server-ip>:9000/health` should answer. If it does not, open the port in the server's firewall (see [Ports and the firewall](operate.md#ports-and-the-firewall)).
2. **The client reaches the server by the same name it used for the API.** If clients reach object storage by another address, set `CAIRNML_STORAGE_PUBLIC_URL` in `deploy/.env` to that address, and run the setup again.
3. **Object storage is healthy** on the server; see the next section.

## Object storage is unreachable

1. `docker compose ps rustfs` shows `healthy` when the storage answers its health check. If not, `docker compose logs rustfs` shows why.
2. `docker compose ps rustfs-init` should show `exited (0)`. The server does not start until the storage setup has finished. If it failed, `docker compose logs rustfs-init` names the step.

## A job from GitHub is refused

The server checks a job from a GitHub repository when it is queued, and refuses it with one of these messages:

| Message | Cause and fix |
|---|---|
| `GitHub is not set up: ...` | The GitHub App is not configured. Set it up as described in the README, section "GitHub App". |
| `the GitHub App is not installed on <owner>/<repo>` | Install the App on that repository, with **Only select repositories**. |
| `no commit is recorded for <owner>/<repo>: commit and push first` | Commit and push the code before you run the job. |
| `commit not found on GitHub: push it first` | The commit is not on GitHub. Push it. |
| `GitHub could not be reached to check the job` | The server cannot reach GitHub. Check the machine's outbound HTTPS, and `GITHUB_API_URL` in `deploy/.env`. |

## Dataset registration is refused

| Message | Cause and fix |
|---|---|
| `GitHub is not configured on this server` | Set up the GitHub App (README, section "GitHub App"). |
| `the dataset registry is not configured on this server` | Set `CAIRNML_DATASET_REGISTRY` in `deploy/.env` (README, section "Datasets"). |
| `datasets of <owner> may not be registered here` | Add the owner to `CAIRNML_DATASET_OWNERS`. |
| `the registry could not be updated (...); the GitHub App needs write access ...` | The App lacks **Contents** and **Pull requests** write access on the registry repository. See README, section "Datasets". |
| `... does not pass its footprint check` | The dataset repository's data does not match its declared footprint. Run `cairnml dataset check` in the dataset repository, fix what it reports, and commit again. |
| `<name> is already registered` | The dataset is in the registry already. |
