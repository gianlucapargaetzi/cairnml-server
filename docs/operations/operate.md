# Operate the server

How to keep the server running: what it stores and where, the ports and the firewall, upgrades, backups, restores and
removal. The commands are run on the server's machine. The stack is the one in `deploy/`, started by
`deploy/scripts/bootstrap.sh`; the symptoms of a failure are in [Troubleshooting](troubleshooting.md).

## What runs and where it is kept

```bash
cd deploy
docker compose ps               # the containers, and which ones are healthy
docker compose logs --tail 100 apiserver
```

The containers are `cairnml-idp` (sign-in), `rustfs` (object storage), `mongo`, `redis`, `elasticsearch`, `apiserver`,
`async-delete` and `webserver` (the proxy and the web app). `rustfs-init` runs once at each start and then exits.

All the state is in one folder, `CAIRNML_HOME` in `deploy/.env`, which defaults to `~/.local/share/cairnml`. The
repository holds no state, so a `git clean` or a new clone does not touch it.

| Folder in `CAIRNML_HOME` | Holds |
|---|---|
| `data/idp` | The sign-in service's database (Keycloak's embedded H2 database): users, the realm's state |
| `data/rustfs` | Object storage: artifacts, models, datasets |
| `data/cairnml/mongo` | The server's database: projects, tasks, users, models |
| `data/cairnml/redis` | Short-lived state, such as the workers and queues |
| `data/cairnml/elastic` | Events and metrics |
| `data/cairnml/logs` | The server's logs |
| `secrets` | The keys and passwords the stack uses |

The `.env` file in `deploy/` is not in that folder. It holds the admin's password and the storage root password, so it is
part of every backup.

**Memory.** With the defaults the whole stack needs a few GiB of RAM, most of it in Elasticsearch. Its JVM heap is set by
`CAIRNML_ES_HEAP` in `deploy/.env` (default `2g`, the same for the minimum and the maximum), so Elasticsearch does not take
half of the machine. With the default it uses about 2.7 GiB in `docker stats`. Change the value and recreate the container
with `docker compose up -d elasticsearch` to apply it.

## Ports and the firewall

| Port | Who reaches it | Setting |
|---|---|---|
| `80/tcp` | Clients and agents: the web app, the API, the sign-in service | `CAIRNML_HTTP_PORT` |
| `9000/tcp` | Clients and agents, for the uploads and downloads that go straight to object storage | `CAIRNML_STORAGE_PORT` |
| `9001/tcp` | This machine only (bound to loopback): the storage console | `CAIRNML_STORAGE_CONSOLE_PORT` |

Open the first two in the machine's firewall. No other port is published: the API, the sign-in service and the databases
are reachable only inside Docker. The server calls GitHub over HTTPS (`GITHUB_API_URL`) when a job from
a GitHub repository is submitted and when a dataset is listed or read, and it asks other Git hosts over HTTPS when a job from
one of them is submitted. The machine needs outbound HTTPS for those.

The first release serves plain HTTP only, for a LAN; TLS is not supported yet. The sign-in service's admin console is
reachable on the same port, so limit that port to the machines that need it. See
[Network and security](../../README.md#network-and-security).

## Upgrade

1. Back up first (see [Backup](#backup)).
2. Update the checkout of this repository to the new version, with no local changes in it.
3. Run the setup again:

   ```bash
   deploy/scripts/bootstrap.sh
   ```

**Downtime.** The setup does not stop the stack, with one exception (below). It rebuilds the images
(`docker compose up -d --build`) and recreates the containers whose image or configuration changed. Data and secrets are
kept. After an upgrade of the server, `apiserver`, `async-delete` and `webserver` are recreated (they share one image),
so the web app and the API are unavailable while they restart. Those three can also be recreated by a run that changes
nothing: with some Docker setups (the containerd image store, for one) a rebuild gives a new image id even when the
content is the same, and compose recreates the containers that use it. The sign-in service and the databases keep running
unless their own image or settings changed. The build time is not downtime, the restart is. The script waits up to three
minutes for the server to answer; if it does not, it still ends without an error, so check `docker compose ps` and the
logs.

**One-time network change.** An installation created before the stack's network got a fixed subnet (`172.30.216.0/24`
in `deploy/compose.yaml`) has the network `cairnml` with the subnet Docker chose at the time. Docker cannot change a
network's subnet while containers use it, so on the first run after the upgrade the setup compares the two. When they
differ it prints what it does and why, stops the whole stack once with `docker compose down` (no `-v`: the data, the
secrets and the images are kept), and starts it again with the network created anew. The web app, the API and the
sign-in are unavailable for the length of that restart, which is longer than a rebuild-only upgrade. An installation
whose network already has the subnet, and a new installation, are not affected, and a second run changes nothing.
Check the stack with `docker compose ps` after the first run, as after any upgrade.

**Data.** The server applies its database migrations when it starts. The folders in `CAIRNML_HOME` are kept.

**Earlier installs.** Three things changed in earlier versions, and the setup handles them on the first run:

- The Keycloak admin password used to be kept in `CAIRNML_HOME/secrets/keycloak-admin-password`. The setup reads it once, as the previous password, and deletes it after a successful run.
- Installs made before the Git credentials feature have no `git_credentials_key` secret, so adding a Git credential for a non-GitHub host fails with `the secret git_credentials_key is missing`. Run the setup once after the upgrade. It creates the key in `CAIRNML_HOME/secrets/cairnml/` with mode 600 and prints only its path. It removes no data, and the running server reads the key on each use, so no service is restarted for this. Keep the key with the backups: stored Git credentials cannot be read without it.
- The CairnML admin's sign-in password is now separate from the Keycloak administrator's password (`CAIRNML_ADMIN_PASSWORD` in `deploy/.env`). The first run after the upgrade generates it, if it is missing, and sets the admin's CairnML password to it. Sign in to CairnML with that value; the Keycloak password does not change.
- The device sign-in is turned on for the `cairnml-sdk` client by the setup, not by hand (see the README, section "Machines without a browser").

If you upgrade from a version older than the current one and a step fails, the setup stops with a message on its own
line; see [Troubleshooting](troubleshooting.md#the-server-does-not-start-or-the-setup-fails).

**Components.** The setup places the SDK, the agent and the web app next to this repository, at the commits named in
`docker/build/`. It removes and clones these folders again when their pin changes (`scripts/fetch-sources.sh`), so do not
keep work in them. To build the web app from your own checkout, set `CAIRNML_WEB_DIR` in `deploy/.env`.

There is no documented way back to an older server version. To go back, restore the backup taken before the upgrade
(see [Restore](#restore)).

## Backup

Back up the whole `CAIRNML_HOME` folder, which includes the secrets, and `deploy/.env`. Take the backup while the stack is
stopped, so that the databases are not copied in the middle of a write.

```bash
STATE="${CAIRNML_HOME:-$HOME/.local/share/cairnml}"   # the value of CAIRNML_HOME in deploy/.env, if you set one
cd deploy
docker compose stop
sudo tar --numeric-owner -C "$(dirname "$STATE")" -czf "$HOME/cairnml-state-$(date +%F).tar.gz" "$(basename "$STATE")"
cp .env "$HOME/cairnml-env-$(date +%F)"
docker compose start
```

- `sudo` is needed: the folders belong to the users of the containers (for example `data/idp` is readable by its owner
  only), and `--numeric-owner` keeps those owners for the restore.
- The backup holds every password and key of the server, and the GitHub App key. Store it like a password manager
  export: encrypted, and only where the people who run the server can reach it.
- The archive is owned by root, because `sudo` wrote it. To copy it off the machine, first run
  `sudo chown "$USER" "$HOME/cairnml-state-$(date +%F).tar.gz"`.
- Keep the backups for as long as you might need to go back to them, and take one before each upgrade.

## Restore

Restore onto the same server version the backup was taken from. To upgrade, restore first and upgrade afterwards.

The datastore part of this procedure was rehearsed once on a throwaway MongoDB container with test data: a backup was
taken with the commands above, the test database was dropped, and the restore gave the data back. The
setup step at the end (`bootstrap.sh`, which starts the whole stack) was not part of that rehearsal. After a restore,
check that `docker compose ps` shows every service healthy and that you can sign in.

Run the commands from the root of the checkout of this repository, on the machine to restore onto, with no stack running:

```bash
# On the machine to restore onto: a checkout of this repository, and no stack running yet.
STATE="${CAIRNML_HOME:-$HOME/.local/share/cairnml}"   # the same folder as in the backup
cp <backup>/cairnml-env-<date> deploy/.env           # the .env of the backup, copied before the first setup
sudo tar --numeric-owner -C "$(dirname "$STATE")" -xzpf <backup>/cairnml-state-<date>.tar.gz
deploy/scripts/bootstrap.sh
```

The setup then starts the stack on the restored state and keeps the restored secrets. If a stack from before the restore
is running, stop it first with `docker compose down` in `deploy/`.

## Deleting a user

`users.delete` deletes one user of the server by id. It is an internal call: only the roles `system` and `root` may make
it. The `admin` and `user` roles are refused, and the web app has no page for it. The IdP's account of the person is
not touched.

**What is removed.**

- The user's profile and the sign-in record the server keeps for them. With the sign-in record gone, the server finds
  no owner on a request and refuses sessions, device tokens and job tokens. A device token also stays refused because
  the devices are revoked, and a job token because its task is stopped. A web token that has not expired yet works
  again for a person the IdP still lists, once their account is created again (at sign-in or by the sync).
- The user's own Git credentials, with their stored secrets, and every assignment of a repository the user holds. The
  company's GitHub Apps the user added are not the user's: they stay and pass to the company's first other admin. If the
  company has no other admin, they stay with the deleted user's id and the server logs a warning.
- The shares granted to the user (a queue shared with them), and their repository checks.
- The user's membership of every project they are a member of, as removing a member does: the shares they granted to
  the project as a member go with them.
- The user's devices are revoked, one audit event for each device that was not revoked already, and kept as history.

**What stays.** Everything the user owns or made keeps naming them, as history: projects they own, queues and
repositories they created, and the tasks, models and datasets they made. The shares they
granted as the owner of a project, and the shares they granted to others as a user, stay too. The audit trail stays, and
the deletion writes one `user.deleted` event as its last step, with who deleted, the user, and how many tasks and
memberships it removed.

**Jobs.** The user's queued tasks are taken out of their queues and set to `stopped`, and so are their running tasks, with
the message `Stopped: its owner was deleted`. Running tasks of other users that run on one of the deleted user's
devices (an agent of theirs) are stopped the same way, because the agent that would report their end is gone. Queued
tasks of other users in the queue of the deleted user's agent stay queued. A running task is set to `stopped` at once,
not to `stopping`: an agent of another user that still polls should end the job (this depends on the agent and is not
tested here), but the process on the deleted user's own agent machine is not killed. Stop it on that machine.

**The person can come back.** The server keys a user by the identity the IdP reports, so the same person always gets
the same user id. If the IdP still lists them as enabled, the server creates a new, empty account for them at their next
sign-in, or by the directory sync (every 60 seconds, `sync_interval_sec`) before that: no membership, no device, no
credential and no assignment of the old one. Because the id is the same, the new account owns again the projects, queues
and repositories the old one owned, if it is created in the same company (the default company). To keep someone out, disable or delete them in the IdP; deleting the user in
CairnML does not.

**A failure halfway.** Every step can be repeated, and the profile is the last record to go. If the call fails, call it
again with the same user: it finishes the rest. The `user.deleted` event is written once, when the call completes. The
call answers `InvalidUserId` when there is no profile left.

**Known limits.**

- The worker records in Redis that name the user stay until the worker timeout runs out.
- Where a screen or an API answer names a user by id (the owner of a queue, the creator of a task, the repository
  views), a deleted user is shown as the raw id.
- Shares granted by the owner of a project stay active after the owner is deleted.
- A user deleted before this behaviour existed may still have a sign-in record and memberships, and no profile. Because
  the call needs the profile, it cannot clean them up.
- Deleting the company's only admin is allowed. The company has no admin until an admin of the IdP signs in or the
  directory sync creates the account.
- If the directory sync creates the account while a delete is running (after the sign-in record went, before the
  profile goes), the new account can end up without a profile until its next sign-in, which makes one.
- If writing the `user.deleted` event fails after the profile is gone, the call cannot be repeated (it answers
  `InvalidUserId`) and the event is lost. On a repeated call the event's counts are those of the repeat only.

## Uninstall

`deploy/scripts/teardown.sh` removes the stack. It asks for the phrase `delete cairnml` unless `--yes` is given.

```bash
deploy/scripts/teardown.sh --dry-run    # print what would be removed; change nothing
deploy/scripts/teardown.sh              # the containers, the volumes and the images; keeps all the data and secrets
deploy/scripts/teardown.sh --data       # also removes the data: users, databases, logs, object storage, artifacts
```

Other options: `--prune-docker` removes every unused image, network and volume on the machine, including those of
other projects. A teardown never removes the secrets, `deploy/.env` or the GitHub App key. The App key cannot be created
again, so keep a copy of it before you remove anything by hand.

The machine's own device key and session (in `~/.cairnml`) are removed by the teardown too. The server keeps the record of
the device until its data is removed.
