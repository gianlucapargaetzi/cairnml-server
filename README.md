<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/cairnml-logo-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="assets/cairnml-logo.svg">
    <img src="assets/cairnml-logo.svg" alt="CairnML" width="480">
  </picture>
</p>

<p align="center">
  <a href="https://github.com/gianlucapargaetzi/cairnml-server/actions/workflows/ci.yml"><img src="https://github.com/gianlucapargaetzi/cairnml-server/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/gianlucapargaetzi/cairnml-server/releases"><img src="https://img.shields.io/github/v/release/gianlucapargaetzi/cairnml-server?style=flat-square&color=blue" alt="Release"></a>
  <a href="https://hub.docker.com/r/gianlucapargaetzi/cairnml-server"><img src="https://img.shields.io/docker/v/gianlucapargaetzi/cairnml-server?style=flat-square&logo=docker&logoColor=white&sort=semver&label=docker%20image" alt="Docker image"></a>
  <a href="https://hub.docker.com/r/gianlucapargaetzi/cairnml-server"><img src="https://img.shields.io/docker/pulls/gianlucapargaetzi/cairnml-server?style=flat-square&logo=docker&logoColor=white&label=docker%20pulls" alt="Docker pulls"></a>
  <a href="https://github.com/gianlucapargaetzi/cairnml-server/stargazers"><img src="https://img.shields.io/github/stars/gianlucapargaetzi/cairnml-server?style=flat-square&logo=github" alt="GitHub stars"></a>
  <a href="https://github.com/gianlucapargaetzi/cairnml-server/blob/main/LICENSE"><img src="https://img.shields.io/github/license/gianlucapargaetzi/cairnml-server?style=flat-square&color=green" alt="License"></a>
</p>

# CairnML Server

CairnML Server is the self-hosted part of CairnML. One script starts everything it needs in Docker: the sign-in service, the API, the web app and object storage. The SDK and the agents connect to it, and every run is recorded on it.

## Features

- Experiment tracking, with runs stored with their parameters, metrics and artifacts.
- Job queues for agents on other machines.
- Object storage for artifacts, models and datasets.
- Sign-in through Keycloak, with one key per machine instead of shared access keys.
- The web app, served from the same host.
- One setup script that detects the server's address and generates its secrets.

## Prerequisites

- Linux
- Docker Engine with Docker Compose v2.20 or newer
- `git`, `openssl`, `python3` and `curl`
- A host that other machines can reach over the network

## Installation

**1. Clone the repository**

```bash
git clone https://github.com/gianlucapargaetzi/cairnml-server.git
cd cairnml-server
```

**2. Choose the address (optional)**

The setup script uses this machine's IPv4 address. If the server has several addresses, set `CAIRNML_HOST` in `deploy/.env` to the one that clients use before the first run.

**3. Start the server**

```bash
deploy/scripts/bootstrap.sh
```

The first run builds the images and takes a while. The script generates the passwords and keys the stack needs and keeps them outside the repository, in `~/.local/share/cairnml`. It is safe to run again, for example after an update.

## First sign-in

1. Open `http://<server-ip>:8080`.
2. Sign in as the user named in `KC_ADMIN` (`cairnadmin` by default), with the password in `deploy/.env`.

Your account is created at the first sign-in.

## Users and admins

Accounts are kept in Keycloak, in the `cairnml` realm. Self-registration is off, so an administrator creates every account. A user is a CairnML admin while they hold the `cairnml-admin` realm role. The server picks up changes to accounts and roles within about a minute.

The server runs over plain HTTP, so use it on a network you trust.

### The admin account

Setup creates the admin named `KC_ADMIN` in `deploy/.env` (`cairnadmin` by default). It is the Keycloak administrator in the `master` realm, and the CairnML admin in the `cairnml` realm. Keycloak also creates a temporary admin, `kcadmin`, on its first start. Setup deletes it from both realms once `KC_ADMIN` has signed in, so no temporary account is left behind.

To change the admin's password, set `KC_ADMIN_PASSWORD` in `deploy/.env` and run `deploy/scripts/bootstrap.sh` again.

### Add users

1. Open the Keycloak admin console at `http://<server-ip>:8180/admin` and sign in as `KC_ADMIN`.
2. Choose the `cairnml` realm in the realm selector at the top left.
3. Go to **Users**, then **Add user**. Enter a username, email and name, and turn on **Email verified**.
4. On the new user's **Credentials** tab, set a password and leave **Temporary** off.

A plain user can sign in and use the server, but cannot do admin tasks. Their CairnML account is created the first time they sign in.

### Make someone admin, or remove the role

On the user's **Role mapping** tab, click **Assign role**, choose **Filter by realm roles** and assign `cairnml-admin`. To remove the role, unassign it on the same tab.

### Disable or remove a user

Switch the user's **Enabled** off in the `cairnml` realm. Their sessions, devices and job tokens stop working. Disabling can be undone; deleting removes the account from Keycloak.

## Connect the clients

The clients use the address of this server:

- **SDK:** `cairnml-init --server http://<server-ip>:8008`, see the [SDK](https://github.com/gianlucapargaetzi/cairnml).
- **Agents:** an admin enrols each agent with `cairnml-agent init`, see the [agent](https://github.com/gianlucapargaetzi/cairnml-agent).

## Next steps

Once the clients are connected, continue with the [CairnML examples](https://github.com/gianlucapargaetzi/cairnml-examples).

## Having problems

1. Check the [open issues](https://github.com/gianlucapargaetzi/cairnml-server/issues).
2. If your problem is not listed, [open a new issue](https://github.com/gianlucapargaetzi/cairnml-server/issues/new) with the command you ran, the output of `docker compose logs`, and the full error message.

## License

Server Side Public License v1, see [LICENSE](LICENSE). Unofficial fork of ClearML, see [DISCLAIMER.md](DISCLAIMER.md).
