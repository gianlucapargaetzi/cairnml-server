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
2. Sign in as the user named in `KC_ADMIN` (`kcadmin` by default), with the password in `deploy/.env`.

Your account is created at the first sign-in.

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
