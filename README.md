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

**2. Start the server**

```bash
deploy/scripts/bootstrap.sh
```

The first run builds the images and takes a while. The script generates the passwords and keys the stack needs and keeps them outside the repository, in `~/.local/share/cairnml`. It is safe to run again, for example after an update. An install from before the Git credentials feature needs one run after the upgrade: it creates the missing `git_credentials_key` secret and removes no data (see [Operate the server](docs/operations/operate.md#upgrade)). It does not stop the stack: it rebuilds the images and recreates only the containers whose image or configuration changed, so those services are down while they restart. See [Operate the server](docs/operations/operate.md).

## First sign-in

1. Open `http://<server-ip>/cairnml/`.
2. Sign in as the user named in `KC_ADMIN` (`cairnadmin` by default), with the password in `deploy/.env`.

Your account is created at the first sign-in.

## Users and admins

Accounts are kept in Keycloak, in the `cairnml` realm. Self-registration is off, so an administrator creates every account. A user is a CairnML admin while they hold the `cairnml-admin` realm role. The server picks up changes to accounts and roles within about a minute.

The server runs over plain HTTP, so use it on a network you trust. See [Network and security](#network-and-security).

### The admin account

Setup creates the admin named `KC_ADMIN` in `deploy/.env` (`cairnadmin` by default). It is the Keycloak administrator in the `master` realm, and the CairnML admin in the `cairnml` realm. Keycloak also creates a temporary admin, `kcadmin`, on its first start. Setup deletes it from both realms once `KC_ADMIN` has signed in, so no temporary account is left behind.

To change the admin's password, set `KC_ADMIN_PASSWORD` in `deploy/.env` and run `deploy/scripts/bootstrap.sh` again. When the password has changed, setup asks in the terminal for the previous one. It is not stored.

### Add users

1. Open the Keycloak admin console at `http://<server-ip>/cairnml/idp/admin` and sign in as `KC_ADMIN`.
2. Choose the `cairnml` realm in the realm selector at the top left.
3. Go to **Users**, then **Add user**. Enter a username, email and name, and turn on **Email verified**.
4. On the new user's **Credentials** tab, set a password and leave **Temporary** off.

A plain user can sign in and use the server, but cannot do admin tasks. Their CairnML account is created the first time they sign in.

### Make someone admin, or remove the role

On the user's **Role mapping** tab, click **Assign role**, choose **Filter by realm roles** and assign `cairnml-admin`. To remove the role, unassign it on the same tab.

### Disable or remove a user

Switch the user's **Enabled** off in the `cairnml` realm. Their new sign-ins are refused, and the devices they own stop working. Disabling can be undone; deleting removes the account from Keycloak.

## GitHub App

Remote execution checks the repository of every job before it is queued. For a repository on `github.com` the check needs a GitHub App: the job is refused until the App is set up and installed on that repository. For a repository on another Git host, a public repository is accepted, and a private one needs Git credentials stored for its host (see [Git credentials for other hosts](#git-credentials-for-other-hosts)).

The server uses one App. Create it once, and install it on every repository that jobs will run from; step 1 says which accounts can hold an installation.

**1. Create the App.** On GitHub, open **Settings → Developer settings → GitHub Apps → New GitHub App** (for an organisation, the same page under the organisation's settings). Fill in:

- **GitHub App name:** any name, for example `cairnml-<your-name>`.
- **Homepage URL:** the server's web address, for example `http://<server-ip>/cairnml/`.
- **Webhook:** untick **Active**. CairnML does not use webhooks.
- **Repository permissions:** set **Contents** and **Pull requests** to **Read and write**. Leave every other permission at **No access**. A job only reads its repository. Dataset registration writes to the registry through a pull request, and the server asks GitHub for write access only at that moment. See [Datasets](#datasets).
- **Where can this GitHub App be installed?** choose **Only on this account**, unless other accounts must use it.

Click **Create GitHub App**. Note the **App ID** shown at the top of the App's page.

**2. Create a private key.** On the App's page, under **Private keys**, click **Generate a private key**. GitHub downloads a `.pem` file.

**3. Install the App on your repositories.** On the App's page, open **Install App**, choose the account, and select **Only select repositories**. Pick every repository that jobs will run from. A repository that is not selected is refused when a job is queued.

**4. Give the App to the server.** Put the App ID in `deploy/.env`:

```
GITHUB_APP_ID=<the App ID>
```

Copy the key to the state directory, which is `~/.local/share/cairnml/secrets/` unless `CAIRNML_HOME` says otherwise:

```bash
mkdir -p ~/.local/share/cairnml/secrets
cp ~/Downloads/<app-name>.*.private-key.pem ~/.local/share/cairnml/secrets/github-app.pem
chmod 600 ~/.local/share/cairnml/secrets/github-app.pem
```

Then run `deploy/scripts/bootstrap.sh` again. It is safe to run again, and it passes the App ID and key to the server.

To check the setup, commit and push a change to a repository the App is installed on, and enqueue a job from it: a job runs only from a commit that GitHub has. A refused job names the step that is missing. Without the App, the message starts with `GitHub is not set up` and names `GITHUB_APP_ID`. When the App is not installed on the repository, it is `the GitHub App is not installed on <owner>/<repo>`. Both messages link to this section. The other refusals are listed in [Troubleshooting](docs/operations/troubleshooting.md#a-job-from-github-is-refused).

### Git credentials for other hosts

A job from a repository on a Git host other than GitHub (for example GitLab) is checked by asking the host, without credentials, whether it serves the repository. A public repository is accepted. A private one, or one the server cannot ask, is refused unless credentials are stored for its host or for a path prefix on it. The agent then gets those credentials when it fetches the repository.

An admin stores them with the `git_credentials.set` call of the API, version 2.8 or later, with a JSON body such as `{"host": "gitlab.example.com", "path": "team/", "username": "<user>", "secret": "<access token>"}`. `path` may be left out, which applies the credential to every repository on the host. The secret is encrypted at rest and is never returned. The web app and the SDK have no screen or command for this yet. `git_credentials.delete` removes an entry, and `git_credentials.get_all` lists the hosts and paths, without secrets.

## Network and security

The first release is for a local network (LAN). It has these limits, and they are the intended behaviour, not bugs:

- **Plain HTTP only.** The stack does not offer TLS: there is no certificate setup and no supported way to put HTTPS in front of it. Sign-in cookies, access tokens and object-storage links travel unencrypted. Use the server only on a network you trust, and do not publish its ports to the internet. The proxy sends no `Strict-Transport-Security` header, because browsers ignore it over plain HTTP.
- **Sign-in service in development mode, with an embedded database.** Keycloak runs with `start-dev` and stores its users and realm in an embedded H2 database, in `data/idp` under `CAIRNML_HOME`. This is the setup for LAN installs. Back up the folder with the stack stopped (see [Operate the server](docs/operations/operate.md#backup)).
- **The Keycloak admin console is on the same address.** It is served at `/cairnml/idp/admin`, so every machine that can reach the web app's port can open it, and sign-in there needs the admin's password. Use the machine's firewall to keep the port to the machines that need it.

Keep `CAIRNML_COOKIE_SECURE` at `false` and `CAIRNML_PUBLIC_ORIGIN` empty unless you know what they do. The first release does not require them.

## Connect the clients

The server needs no address of its own: it answers to any address of this machine (`localhost`, its IP address or its host name), and names itself after the address the client used. Everything is under `/cairnml` of that address, so the root of the machine stays free for other sites:

| What | Address |
|---|---|
| Web app | `http://<server-ip>/cairnml/` |
| API | `http://<server-ip>/cairnml/api` |
| Sign-in service (Keycloak) | `http://<server-ip>/cairnml/idp/` |

The clients take the address of the server and work out the rest:

- **SDK:** `cairnml init http://<server-ip>`, see the [SDK](https://github.com/gianlucapargaetzi/cairnml).
- **Agents:** an admin enrols each agent with `cairnml-agent init http://<server-ip>`, see the [agent](https://github.com/gianlucapargaetzi/cairnml-agent).

Opening `http://<server-ip>/` leads to the web app. Nothing else is served at the root.

### Machines without a browser

On a machine with no browser, such as a headless server, a remote shell or a container, `cairnml init` and `cairnml-agent init` show a short code and an address. Open the address on any device, sign in, and enter the code. Add `--device` to use this sign-in on any machine. On Linux without a graphical session it is used automatically.

The device sign-in is on for the `cairnml-sdk` client. Existing installs get it by running `deploy/scripts/bootstrap.sh` again, which applies the CairnML clients from `deploy/idp/realm-cairnml.json`. No change in the admin console is needed.

### Ports

The stack publishes three ports on the machine. They are the defaults, and each can be changed in `deploy/.env` when something else already uses it. The server follows: the proxy listens on the new port, and the addresses the server gives to clients name it.

| Setting in `deploy/.env` | What | Default |
|---|---|---|
| `CAIRNML_HTTP_PORT` | The address clients use: the web app, the API and the sign-in service. The address then needs the port, for example `http://<server-ip>:8081/cairnml/`, and so does the argument of `cairnml init`. | `80` |
| `CAIRNML_STORAGE_PORT` | Object storage, which clients reach through presigned URLs | `9000` |
| `CAIRNML_STORAGE_CONSOLE_PORT` | The storage console, on this machine only | `9001` |

Two more ports are not published: the API and the sign-in service listen only inside the stack's own network and are reached through the proxy, so nothing on the machine can clash with them. You can still change them, and the proxy, the health checks and the setup script follow.

| Setting in `deploy/.env` | What | Default |
|---|---|---|
| `CAIRNML_API_PORT` | The API, inside the stack | `8008` |
| `CAIRNML_IDP_PORT` | The sign-in service (Keycloak), inside the stack | `8180` |

Run `deploy/scripts/bootstrap.sh` again after changing a port.

The server builds the addresses it hands out (sign-in redirects, the identity provider's issuer, storage links) from the address the proxy received. It believes the forwarded address headers only from the proxies in `CAIRNML_TRUSTED_PROXIES`, which by default are loopback and the Docker network of the stack. If another reverse proxy sits in front of the stack, add its range there. A deployment with a fixed address sets `CAIRNML_PUBLIC_ORIGIN`: the server then ignores the Host of every request.

Clients and agents reach the stack on the first two ports, so the machine's firewall must let them in on `CAIRNML_HTTP_PORT` and `CAIRNML_STORAGE_PORT`. The storage console on `CAIRNML_STORAGE_CONSOLE_PORT` answers on loopback only.

## Datasets

The dataset registry lets `cairnml dataset register` add a dataset repository to a registry repository, through a pull request that the server opens. It needs the GitHub App above, and three things more:

1. **Permissions.** Step 1 above already sets **Contents** and **Pull requests** to **Read and write**, which registration needs. Jobs alone need only read access. If you change the permissions of an App that is already installed, GitHub asks the owner of each installation to accept the change.
2. **Installation.** Install the App on the registry repository and on every dataset repository.
3. **Settings.** In `deploy/.env`, name the registry and the owners whose dataset repositories may be registered in it:

   ```
   CAIRNML_DATASET_REGISTRY=<owner>/<registry-repository>
   CAIRNML_DATASET_OWNERS='["<owner>"]'
   ```

Then run `deploy/scripts/bootstrap.sh` again. The registry also lists the dataset repositories that a job may clone, so only add repositories you trust to it.

Without the App, `cairnml dataset register` fails with `GitHub is not configured on this server`. With the App but without `CAIRNML_DATASET_REGISTRY`, it fails with `the dataset registry is not configured on this server`. An owner that is not listed fails with `datasets of <owner> may not be registered here`.

## Settings reference

The settings a user may set are in `deploy/.env`, copied from [`deploy/.env.example`](deploy/.env.example) by the setup script. Anything not listed here is internal to the stack.

| Setting | Default | What it is for |
|---|---|---|
| `KC_ADMIN` | `cairnadmin` | The admin's name, in Keycloak and in CairnML. Must not be `kcadmin`. See [First sign-in](#first-sign-in). |
| `KC_ADMIN_PASSWORD` | generated | The admin's password. Change it, then run `deploy/scripts/bootstrap.sh` again. |
| `CAIRNML_HTTP_PORT` | `80` | The port of the one address clients use. See [Ports](#ports). |
| `CAIRNML_STORAGE_PORT` | `9000` | The port of object storage, which clients reach. See [Ports](#ports). |
| `CAIRNML_STORAGE_CONSOLE_PORT` | `9001` | The port of the storage console, on loopback only. |
| `CAIRNML_STORAGE_PUBLIC_URL` | empty | The address clients use for object storage, when it differs from the address they used for the server. Empty: the same address, on `CAIRNML_STORAGE_PORT`. |
| `CAIRNML_S3_ACCESS_KEY` | `cairnml-server` | The object storage user of the server. Its secret is in `secrets/cairnml/s3_secret_key`. |
| `RUSTFS_ROOT_USER` | `rustfsadmin` | The root user of object storage, used by its setup job. |
| `RUSTFS_ROOT_PASSWORD` | generated | The password of that root user. Keep it secret: the storage port is open to the network. |
| `CAIRNML_HOME` | `~/.local/share/cairnml` | Where the data and secrets are kept. See [Operate the server](docs/operations/operate.md). |
| `CAIRNML_API_PORT` | `8008` | The API, inside the stack. |
| `CAIRNML_IDP_PORT` | `8180` | The sign-in service, inside the stack. |
| `CAIRNML_COOKIE_SECURE` | `false` | Keep `false`: the first release serves plain HTTP only. See [Network and security](#network-and-security). |
| `CAIRNML_PUBLIC_ORIGIN` | empty | A fixed address for the stack, with scheme and port, when clients should not use the address they typed. Empty for a LAN. See [Connect the clients](#connect-the-clients). |
| `CAIRNML_TRUSTED_PROXIES` | `["127.0.0.0/8","::1/128","172.16.0.0/12"]` | The JSON list of proxies whose forwarded address headers the server believes. Headers from other addresses are ignored and logged. Add the range of a reverse proxy in front of the stack. See [Ports](#ports). |
| `GITHUB_APP_ID` | empty | The GitHub App. See [GitHub App](#github-app). |
| `GITHUB_API_URL` | `https://api.github.com` | The GitHub API the App uses. Change it only for GitHub Enterprise Server. |
| `CAIRNML_DATASET_REGISTRY` | empty | The dataset registry repository. See [Datasets](#datasets). |
| `CAIRNML_DATASET_OWNERS` | `[]` | The owners whose dataset repositories may be registered. See [Datasets](#datasets). |
| `CAIRNML_WEB_DIR` | empty | A checkout of the web app to build instead of the pinned one. See `scripts/fetch-sources.sh`. |

## Compatibility

Each component has its own version number, and the numbers are not related: server 2.4.0 does not mean SDK 2.4.0. The server image is built from one commit of each component, named by the pin file in `docker/build/`. The versions at the commits this repository pins are:

| Component | Repository | Pin file | Version at the pinned commit |
|---|---|---|---|
| Server | this repository | the commit you run | `apiserver/version.py` (2.4.0 here) |
| SDK (`cairnml`) | `cairnml` | `docker/build/sdk-ref` | 0.0.2 |
| Agent (`cairnml-agent`) | `cairnml-agent` | `docker/build/agent-ref` | 0.0.1 |
| Web app | `cairnml-web` | `docker/build/web-ref` | 2.5.0 |

The pins are the only record of which versions go together. This repository's tests do not check other combinations.

- **Use the SDK and the agent at the commits the pins name.** A client installed from another source may work, but it is not tested with this server.
- **Old clients get a warning, not a refusal.** The SDK and the agent send their name and version in the `X-CairnML-Client` header. When a client is older than the minimum set in `apiserver.client_versions` (`min_sdk_version` 0.0.2, `min_agent_version` 0.0.1 in the shipped configuration), the server still serves it, adds an `X-CairnML-Upgrade` header to each response that says what to upgrade, and logs the warning once in `docker compose logs apiserver`.
- **API version.** Clients name the API version in the address (`/cairnml/api/v2.8/...`). The device sign-in (enrolment, token exchange, job tokens) needs API version 2.8 or later, so a client that uses only older API versions cannot enrol.

## Running the server's tests

The unit tests are the ones CI runs (`.github/workflows/ci.yml`), on Python 3.11, as in the image:

```bash
python3 -m pip install --require-hashes -r apiserver/requirements.lock
python3 -m pip install pytest
python3 -m pytest -q -m "not integration" apiserver/tests
```

Most of the unit tests use MongoDB, which must run on `127.0.0.1:27017` (the default). Without it they wait for the connection to time out, so start MongoDB first. The tests marked `needs_redis` are skipped when no Redis answers on the configured address (`localhost:6379` by default), so Redis is optional for a local run. The tests marked `integration` need Elasticsearch and a running server; they run in the end-to-end suite, see [E2E.md](docs/e2e_testing/E2E.md).

## Architecture

The flows of the common operations are in [`docs/architecture/flows/`](docs/architecture/flows/README.md): sign-in, device enrolment, token exchange, job tokens, job execution, task logging, database management and the trust model. Each page has a sequence diagram.

## Next steps

Once the clients are connected, continue with the [CairnML examples](https://github.com/gianlucapargaetzi/cairnml-examples).

## Operate the server

Upgrading, backing up and restoring the server, its ports and firewall, and removing it are in [Operate the server](docs/operations/operate.md).

## Having problems

1. Look up the symptom in [Troubleshooting](docs/operations/troubleshooting.md).
2. Check the [open issues](https://github.com/gianlucapargaetzi/cairnml-server/issues).
3. If your problem is not listed, [open a new issue](https://github.com/gianlucapargaetzi/cairnml-server/issues/new) with the command you ran, the output of `docker compose logs`, and the full error message.

## License

Server Side Public License v1, see [LICENSE](LICENSE). Unofficial fork of ClearML, see [DISCLAIMER.md](DISCLAIMER.md).
