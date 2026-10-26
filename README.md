# 🪨 CairnML Server

Self-hosted server for experiment tracking, remote jobs and datasets.

## 🧰 Prerequisites

- 🐧 Linux
- 🐳 Docker Engine with Docker Compose v2.20 or newer
- 🔧 `git`, `openssl`, `python3` and `curl`
- 🔑 Permission to edit `/etc/hosts`

## 📦 Installation

**1. Clone the repository**

```bash
git clone https://github.com/gianlucapargaetzi/cairnml-server.git
cd cairnml-server
```

**2. Add the host names to `/etc/hosts`**

```
127.0.0.1 cairnml-idp cairnml
```

Every other machine that connects needs the same two names, pointing to the server's address instead of `127.0.0.1`.

**3. Start the server**

```bash
deploy/scripts/bootstrap.sh
```

The first run builds the images and takes a while. The script is safe to run again.

## 🔑 First sign-in

1. Open `http://cairnml:8080`.
2. Sign in as the user in `KC_ADMIN` (`kcadmin` by default), with the password from `deploy/.env`.

Your account is created at the first sign-in.

## ✅ Setup complete

Your server is running. Connect the parts that use it:

- 🐍 **SDK:** [cairnml](https://github.com/gianlucapargaetzi/cairnml), with `cairnml-init --server http://cairnml:8008`
- 🤖 **Agent:** [cairnml-agent](https://github.com/gianlucapargaetzi/cairnml-agent), enrolled by an admin with `cairnml-agent init`

Then continue with **[cairnml-examples](https://github.com/gianlucapargaetzi/cairnml-examples)**.

## 🆘 Having problems

1. Check the [open issues](https://github.com/gianlucapargaetzi/cairnml-server/issues).
2. If your problem is not listed, [open a new issue](https://github.com/gianlucapargaetzi/cairnml-server/issues/new) with the command you ran and the full error message.

## 📄 License

Server Side Public License v1, see [LICENSE](LICENSE). Unofficial fork of ClearML, see [DISCLAIMER.md](DISCLAIMER.md).
