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
| `secrets` | The keys and passwords the stack uses, and the GitHub App key (`github-app.pem`) |

The `.env` file in `deploy/` is not in that folder. It holds the admin's password and the storage root password, so it is
part of every backup.

## Ports and the firewall

| Port | Who reaches it | Setting |
|---|---|---|
| `80/tcp` | Clients and agents: the web app, the API, the sign-in service | `CAIRNML_HTTP_PORT` |
| `9000/tcp` | Clients and agents, for the uploads and downloads that go straight to object storage | `CAIRNML_STORAGE_PORT` |
| `9001/tcp` | This machine only (bound to loopback): the storage console | `CAIRNML_STORAGE_CONSOLE_PORT` |

Open the first two in the machine's firewall. No other port is published: the API, the sign-in service and the databases
are reachable only inside Docker. The server calls GitHub over HTTPS (`GITHUB_API_URL`) when a job from
a GitHub repository is submitted and when a dataset is registered, and it asks other Git hosts over HTTPS when a job from
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

**Downtime.** The setup does not stop the stack. It rebuilds the images (`docker compose up -d --build`) and recreates
only the containers whose image or configuration changed. After an upgrade of the server, `apiserver`, `async-delete` and
`webserver` are recreated (they share one image), so the web app and the API are unavailable while they restart. The
build time is not downtime, the restart is. The sign-in service and the databases keep running unless their own image or
settings changed. The script waits up to three minutes for the server to answer; if it does not, it still ends without an
error, so check `docker compose ps` and the logs.

**Data.** The server applies its database migrations when it starts. The folders in `CAIRNML_HOME` are kept.

**Earlier installs.** Three things changed in earlier versions, and the setup handles them on the first run:

- The GitHub App key used to be kept in `deploy/secrets/github-app.pem`. The setup copies it to `CAIRNML_HOME/secrets/` once, and leaves the old copy in place. Delete the old copy once you have checked the new one.
- The Keycloak admin password used to be kept in `CAIRNML_HOME/secrets/keycloak-admin-password`. The setup reads it once, as the previous password, and deletes it after a successful run.
- Installs made before the Git credentials feature have no `git_credentials_key` secret, so adding a Git credential for a non-GitHub host fails with `the secret git_credentials_key is missing`. Run the setup once after the upgrade. It creates the key in `CAIRNML_HOME/secrets/cairnml/` with mode 600 and prints only its path. It removes no data, and the running server reads the key on each use, so no service is restarted for this. Keep the key with the backups: stored Git credentials cannot be read without it.
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
