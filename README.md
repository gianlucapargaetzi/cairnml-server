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

# 🪨 CairnML Server

Self-hosted server for experiment tracking, remote jobs and datasets.

## 🧰 Prerequisites

- 🐧 Linux
- 🐳 Docker Engine with Docker Compose v2.20 or newer
- 🔧 `git`, `openssl`, `python3` and `curl`

## 📦 Installation

**1. Clone the repository**

```bash
git clone https://github.com/gianlucapargaetzi/cairnml-server.git
cd cairnml-server
```

**2. Start the server**

```bash
deploy/scripts/bootstrap.sh
```

The script uses this machine's IP address. If the server has several addresses, set `CAIRNML_HOST` in `deploy/.env` to the one that clients use, before the first run. The first run builds the images and takes a while. The script is safe to run again.

## 🔑 First sign-in

1. Open `http://<server-ip>:8080`.
2. Sign in as the user in `KC_ADMIN` (`kcadmin` by default), with the password from `deploy/.env`.

Your account is created at the first sign-in.

## ✅ Setup complete

Your server is running. Connect the parts that use it:

- 🐍 **SDK:** [cairnml](https://github.com/gianlucapargaetzi/cairnml), with `cairnml-init --server http://<server-ip>:8008`
- 🤖 **Agent:** [cairnml-agent](https://github.com/gianlucapargaetzi/cairnml-agent), enrolled by an admin with `cairnml-agent init`

Then continue with **[cairnml-examples](https://github.com/gianlucapargaetzi/cairnml-examples)**.

## 🆘 Having problems

1. Check the [open issues](https://github.com/gianlucapargaetzi/cairnml-server/issues).
2. If your problem is not listed, [open a new issue](https://github.com/gianlucapargaetzi/cairnml-server/issues/new) with the command you ran and the full error message.

## 📄 License

Server Side Public License v1, see [LICENSE](LICENSE). Unofficial fork of ClearML, see [DISCLAIMER.md](DISCLAIMER.md).
