"""The suite's own stack: started for a test session, and removed at its end, or kept for the next one.

The stack is a separate compose project (tests/e2e/stack/compose.yaml) with:
- its own state folder (data, secrets, the realm copy), created in a temporary directory;
- its own accounts: the admin's password is generated for the stack;
- its own ports, on the loopback address, so nothing needs /etc/hosts and nothing is shared with the stack under
  deploy/.

Only the Docker daemon and the image cache are shared with the rest of the machine. The cairnml-server image is
the one the deploy/ stack builds; the suite does not change it.

Reuse (CAIRNML_E2E_REUSE_STACK=1, see docs/e2e_testing/E2E.md, speed section item 1): the stack keeps its state, ports and accounts in
REUSE_DIR and is left running at the end of the session. The next session finds it healthy and starts at once. It is
rebuilt and restarted only when the server or web sources, or the compose file, have changed since it was started.
Stop it with `python -m support.stack --stop` (run in tests/e2e).
"""

import fcntl
import hashlib
import json
import os
import random
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import requests

from . import settings

PROJECT_PREFIX = "cairnml-e2e"
REPO = Path(__file__).resolve().parents[3]
COMPOSE_FILE = Path(__file__).resolve().parents[1] / "stack" / "compose.yaml"
REALM_TEMPLATE = REPO / "deploy" / "idp" / "realm-cairnml.json"
STORAGE_SCRIPT = REPO / "deploy" / "storage" / "init.py"
NGINX_TEMPLATE = REPO / "docker" / "build" / "internal_files" / "cairnml.conf.template"
WEB_DIR = Path(os.environ.get("CAIRNML_WEB_DIR") or REPO / "web")
SOURCE_REPOS = (REPO, REPO / "cairnml", REPO / "cairnml-agent", WEB_DIR)
ADMIN_NAME = "e2e-admin"

# A reused stack lives here: its state folder, its ports and accounts (stack.json), and the lock for starting it.
REUSE_DIR = Path(os.environ.get("CAIRNML_E2E_STATE_DIR") or settings.REAL_HOME / ".cache" / "cairnml-e2e-stack")
REUSE_FILE = REUSE_DIR / "stack.json"
START_LOCK = REUSE_DIR / "start.lock"

# Owners of the data folders inside the containers (the same as deploy/scripts/bootstrap.sh).
DATA_OWNERS = {"idp": "1000:1000", "rustfs": "10001:10001", "mongo": "999:999", "redis": "999:999",
               "elastic": "1000:1000"}


class StackError(Exception):
    pass


# The stack runs on the loopback interface only, so the same addresses work for the containers (host networking),
# the suite, the browser and the agent. Nothing depends on this machine's network address.
LOOPBACK = "127.0.0.1"
# Where the stack is reached from the suite, the browser and the agent (HOST), and where its services listen (BIND).
# On the host both are the loopback address. In the isolated environment (tests/e2e/docker) the stack runs in the nested
# daemon, so HOST is that container's name and BIND is every address of it.
HOST = os.environ.get("CAIRNML_E2E_HOST") or LOOPBACK
BIND = os.environ.get("CAIRNML_E2E_BIND") or LOOPBACK


def reuse_requested() -> bool:
    return os.environ.get("CAIRNML_E2E_REUSE_STACK", "").lower() in ("1", "true", "yes")


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def pick_ports(host: str, count: int = 9) -> list:
    chosen = []
    candidates = list(range(18000, 19000))
    random.shuffle(candidates)
    for port in candidates:
        if port in chosen or not _port_free(host, port):
            continue
        chosen.append(port)
        if len(chosen) == count:
            return chosen
    raise StackError("no free ports for the test stack")


def source_fingerprint() -> str:
    """What the stack was built from: the commits and the uncommitted changes of the server and web sources, and the
    compose file. A stack started from other sources is rebuilt; the data is kept."""
    digest = hashlib.sha256()
    for repo in SOURCE_REPOS:
        for args in (["rev-parse", "HEAD"], ["diff", "HEAD"], ["status", "--porcelain"]):
            digest.update(subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=120).stdout)
    digest.update(COMPOSE_FILE.read_bytes())
    return digest.hexdigest()


class IsolatedStack:
    def __init__(self, keep: bool = False, reuse: bool = None):
        self.keep = keep
        self.reuse = reuse_requested() if reuse is None else reuse
        self.host = HOST
        self.job_token_ttl_sec = 3600
        saved = self._read_saved() if self.reuse else None
        if saved:
            self.state = Path(saved["state"])
            self.ports = saved["ports"]
            self.admin_password = saved["admin_password"]
            self.rustfs_password = saved["rustfs_password"]
            self.s3_access = saved["s3_access"]
            self.project = saved["project"]
            self.started_from = saved.get("fingerprint")
        else:
            if self.reuse:
                # A state folder without its record is from a start that did not finish: its accounts are unknown.
                self._remove_state_folder(REUSE_DIR / "state")
                REUSE_DIR.mkdir(parents=True, exist_ok=True)
                self.state = REUSE_DIR / "state"
            else:
                self.state = Path(tempfile.mkdtemp(prefix="cairnml-e2e-stack-"))
            api, web, idp, idp_mgmt, storage, mongo, redis, es, es_transport = pick_ports(BIND)
            self.ports = {"api": api, "web": web, "idp": idp, "idp_mgmt": idp_mgmt, "storage": storage,
                          "mongo": mongo, "redis": redis, "es": es, "es_transport": es_transport}
            self.admin_password = secrets.token_urlsafe(18)
            self.rustfs_password = secrets.token_urlsafe(18)
            self.s3_access = "e2e-server"
            # One compose project per stack: two stacks never share containers or networks.
            self.project = f"{PROJECT_PREFIX}-{secrets.token_hex(3)}"
            self.started_from = None
        self.fingerprint = None

    # --- the reused stack's record -----------------------------------------------------------------------------------

    @staticmethod
    def _read_saved():
        if not REUSE_FILE.exists():
            return None
        return json.loads(REUSE_FILE.read_text())

    def _save(self, fingerprint: str) -> None:
        REUSE_DIR.mkdir(parents=True, exist_ok=True)
        record = {"state": str(self.state), "ports": self.ports, "admin_password": self.admin_password,
                  "rustfs_password": self.rustfs_password, "s3_access": self.s3_access, "project": self.project,
                  "fingerprint": fingerprint}
        REUSE_FILE.write_text(json.dumps(record))
        REUSE_FILE.chmod(0o600)

    @contextmanager
    def _start_lock(self):
        """Two workers (or two sessions) that find the reused stack down must not start it twice."""
        if not self.reuse:
            yield
            return
        REUSE_DIR.mkdir(parents=True, exist_ok=True)
        with open(START_LOCK, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    # --- URLs as the suite (and the containers) see them -----------------------------------------------------------

    @property
    def api_url(self) -> str:
        return f"http://{self.host}:{self.ports['api']}"

    @property
    def web_url(self) -> str:
        return f"http://{self.host}:{self.ports['web']}"

    @property
    def idp_url(self) -> str:
        return f"http://{self.host}:{self.ports['idp']}"

    @property
    def storage_url(self) -> str:
        return f"http://{self.host}:{self.ports['storage']}"

    def configure_settings(self) -> None:
        settings.configure(api=self.api_url, web=self.web_url, idp=self.idp_url, storage=self.storage_url,
                           admin_user=ADMIN_NAME, admin_password=self.admin_password, project=self.project)

    # --- files -----------------------------------------------------------------------------------------------------

    def prepare(self) -> None:
        for folder in ("data/idp", "data/rustfs", "data/mongo/db", "data/mongo/configdb", "data/redis",
                       "data/elastic", "data/logs", "secrets"):
            (self.state / folder).mkdir(parents=True, exist_ok=True)
        for name in ("token_secret", "session_secret", "s3_secret_key"):
            path = self.state / "secrets" / name
            if self.reuse and path.exists():
                continue  # a reused stack keeps its secrets: the server's tokens stay valid across sessions
            path.write_text(secrets.token_urlsafe(48))
            path.chmod(0o600)
        shutil.copy(STORAGE_SCRIPT, self.state / "init.py")
        self._write_nginx_template()
        self._write_realm()
        # No configuration.json: the web app finds its API on its own origin (cairnml-web, app.constants.ts), so the
        # UI tests check that default. The folder stays, as the web server mounts it.
        (self.state / "configs").mkdir(exist_ok=True)

    def _write_nginx_template(self) -> None:
        """The web server's nginx template, as the server image has it, listening on this stack's port."""
        text = NGINX_TEMPLATE.read_text()
        text = text.replace("listen       8080 default_server;", f"listen       {BIND}:{self.ports['web']} default_server;")
        text = text.replace("listen       [::]:8080 default_server;", "")
        folder = self.state / "nginx"
        folder.mkdir(exist_ok=True)
        (folder / "cairnml.conf.template").write_text(text)

    def _write_realm(self) -> None:
        """The committed realm, with this stack's addresses in place of the deploy/ names."""
        text = REALM_TEMPLATE.read_text()
        text = text.replace("http://cairnml:8008", self.api_url)
        text = text.replace("http://cairnml:8080", self.web_url)
        json.loads(text)  # the result must still be a realm
        (self.state / "realm-cairnml.json").write_text(text)

    def _env(self) -> dict:
        return {
            **os.environ,
            "COMPOSE_PROJECT_NAME": self.project,
            "E2E_IDP_MANAGEMENT_PORT": str(self.ports["idp_mgmt"]),
            "E2E_MONGO_PORT": str(self.ports["mongo"]),
            "E2E_REDIS_PORT": str(self.ports["redis"]),
            "E2E_ES_PORT": str(self.ports["es"]),
            "E2E_ES_TRANSPORT_PORT": str(self.ports["es_transport"]),
            "E2E_STATE": str(self.state),
            "E2E_HOST": self.host,
            "E2E_BIND": BIND,
            "E2E_API_PORT": str(self.ports["api"]),
            "E2E_WEB_PORT": str(self.ports["web"]),
            "E2E_IDP_PORT": str(self.ports["idp"]),
            "E2E_STORAGE_PORT": str(self.ports["storage"]),
            "E2E_KC_ADMIN": ADMIN_NAME,
            "E2E_KC_ADMIN_PASSWORD": self.admin_password,
            "E2E_RUSTFS_USER": "e2e-rustfs",
            "E2E_RUSTFS_PASSWORD": self.rustfs_password,
            "E2E_S3_ACCESS_KEY": self.s3_access,
            "E2E_JOB_TOKEN_TTL_SEC": str(self.job_token_ttl_sec),
        }

    # --- compose ---------------------------------------------------------------------------------------------------

    def compose(self, *args, timeout=900) -> subprocess.CompletedProcess:
        return subprocess.run(["docker", "compose", "-p", self.project, "-f", str(COMPOSE_FILE), *args],
                              env=self._env(), capture_output=True, text=True, timeout=timeout)

    def set_job_token_ttl(self, seconds: int) -> None:
        """Restarts only the API with another job token lifetime. The data and the other services are kept; the
        API is back at the default once this is called with 3600 again."""
        self.job_token_ttl_sec = seconds
        result = self.compose("up", "-d", "--no-deps", "apiserver", timeout=300)
        if result.returncode != 0:
            raise StackError(f"cannot restart the API with a job token lifetime of {seconds} s:\n{result.stderr[-3000:]}")
        self._wait(self._api_answers, 120, "the CairnML API after the restart")

    def _chown_data(self) -> None:
        """The containers run as their own users; the folders must be theirs. Done in a container, as bootstrap does."""
        for folder, owner in (("idp", DATA_OWNERS["idp"]), ("rustfs", DATA_OWNERS["rustfs"]),
                              ("mongo", DATA_OWNERS["mongo"]), ("redis", DATA_OWNERS["redis"]),
                              ("elastic", DATA_OWNERS["elastic"])):
            subprocess.run(["docker", "run", "--rm", "-v", f"{self.state}/data:/data", "alpine:3.20", "sh", "-c",
                            f"chown -R {owner} /data/{folder}"], capture_output=True, timeout=300)

    def _chown_for_server(self) -> None:
        """The server's containers run as uid 1000: their logs and secrets must be theirs. Done in a container, as bootstrap does."""
        for folder in ("data/logs", "secrets"):
            subprocess.run(["docker", "run", "--rm", "-v", f"{self.state}/{folder}:/target", "alpine:3.20", "sh", "-c",
                            "chown -R 1000:1000 /target"], capture_output=True, timeout=300)

    def healthy(self) -> bool:
        """Whether the stack answers now. Checked with short timeouts: a stack that is down fails at once."""
        checks = (
            lambda: requests.post(f"{self.api_url}/v2.8/login.supported_modes", json={}, timeout=3).status_code == 200,
            lambda: requests.get(self.web_url + "/", timeout=3).status_code == 200,
            lambda: requests.get(f"{self.idp_url}/realms/cairnml/.well-known/openid-configuration",
                                 timeout=3).status_code == 200,
            lambda: requests.get(self.storage_url + "/health", timeout=3).status_code == 200,
        )
        for check in checks:
            try:
                if not check():
                    return False
            except requests.RequestException:
                return False
        return True

    def start(self) -> None:
        self.configure_settings()
        self.fingerprint = source_fingerprint() if self.reuse else None
        if self.reuse and self.healthy() and self.started_from == self.fingerprint:
            return  # kept running from an earlier session, built from the same sources
        with self._start_lock():
            if self.reuse and self.healthy() and (self._read_saved() or {}).get("fingerprint") == self.fingerprint:
                return  # another worker or session started it while this one waited for the lock
            self.prepare()
            if not self.reuse or not self._initialised():
                self._chown_data()
            result = self.compose("up", "-d", "idp")
            if result.returncode != 0:
                raise StackError(f"cannot start the sign-in service:\n{result.stderr[-3000:]}")
            self._wait(lambda: requests.get(f"{self.idp_url}/realms/cairnml/.well-known/openid-configuration",
                                            timeout=10).status_code == 200, 240, "the sign-in service")
            self._identity()
            self._chown_for_server()  # after _identity, which writes the directory secret
            result = self.compose("up", "-d", "--build")
            if result.returncode != 0:
                raise StackError(f"cannot start the CairnML server:\n{result.stderr[-3000:]}")
            self._wait(self._api_answers, 600, "the CairnML API")
            self._wait(lambda: requests.get(self.web_url + "/", timeout=10).status_code == 200, 300, "the web app")
            if self.reuse:
                self._save(self.fingerprint)

    def _initialised(self) -> bool:
        saved = self._read_saved()
        return bool(saved) and saved.get("state") == str(self.state)

    def _identity(self) -> None:
        """The admin account (a realm user with the cairnml-admin role) and the secret the server reads users with."""
        from .keycloak import Keycloak

        kc = Keycloak()
        kc.create_user(ADMIN_NAME, self.admin_password, admin=True)
        clients = kc._admin("GET", "/clients", params={"clientId": "cairnml-server"}).json()
        client_uuid = clients[0]["id"]
        # The server reads users (for the directory sync) as its service account: it needs view-users, as bootstrap.sh
        # grants it. Without it the sync never sees a disabled account.
        service_user = kc._admin("GET", f"/clients/{client_uuid}/service-account-user").json()["id"]
        realm_management = kc._admin("GET", "/clients", params={"clientId": "realm-management"}).json()[0]["id"]
        view_users = kc._admin("GET", f"/clients/{realm_management}/roles/view-users").json()
        kc._admin("POST", f"/users/{service_user}/role-mappings/clients/{realm_management}",
                  json=[view_users]).raise_for_status()
        secret = kc._admin("GET", f"/clients/{client_uuid}/client-secret").json()["value"]
        (self.state / "secrets" / "idp_directory_secret").write_text(secret)
        (self.state / "secrets" / "idp_directory_secret").chmod(0o600)

    def _api_answers(self) -> bool:
        try:
            return requests.post(f"{self.api_url}/v2.8/login.supported_modes", json={}, timeout=10).status_code == 200
        except requests.RequestException:
            return False

    def _wait(self, check, timeout: float, what: str) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if check():
                    return
            except requests.RequestException:
                pass
            time.sleep(1)
        logs = self.compose("logs", "--tail", "40").stdout[-3000:]
        raise StackError(f"{what} did not become ready in {timeout:.0f} s. Recent logs:\n{logs}")

    def stop(self) -> None:
        if self.reuse:
            print(f"\nthe test stack is kept running for the next session: {self.api_url} "
                  f"(stop it with: python -m support.stack --stop)")
            return
        if self.keep:
            print(f"\nthe test stack is kept: {self.api_url} (state folder {self.state})")
            return
        self.compose("down", "--volumes", "--remove-orphans", timeout=600)
        self._remove_networks()
        self._remove_state()

    @classmethod
    def stop_reused(cls) -> None:
        """Stops and removes the reused stack, with its state and record."""
        if not REUSE_FILE.exists():
            print("no reused test stack")
            return
        stack = cls(reuse=True)
        stack.compose("down", "--volumes", "--remove-orphans", timeout=600)
        stack._remove_networks()
        stack._remove_state()
        REUSE_FILE.unlink(missing_ok=True)
        print("the reused test stack is removed")

    def _remove_networks(self) -> None:
        """Removes this stack's networks that no container uses. A run that failed early can leave them behind."""
        subprocess.run(["docker", "network", "prune", "-f", "--filter",
                        f"label=com.docker.compose.project={self.project}"], capture_output=True, timeout=120)

    def _remove_state(self) -> None:
        self._remove_state_folder(self.state)

    @staticmethod
    def _remove_state_folder(folder: Path) -> None:
        """The containers wrote files as their own users; give them back to this user before removing them."""
        if not folder.exists():
            return
        subprocess.run(["docker", "run", "--rm", "-v", f"{folder}:/state", "alpine:3.20", "sh", "-c",
                        f"chown -R {os.getuid()}:{os.getgid()} /state"], capture_output=True, timeout=300)
        shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    if "--stop" in sys.argv:
        IsolatedStack.stop_reused()
    elif "--status" in sys.argv:
        saved = IsolatedStack._read_saved()
        if not saved:
            print("no reused test stack")
        else:
            stack = IsolatedStack(reuse=True)
            print(f"reused test stack at {stack.api_url}: {'answering' if stack.healthy() else 'not answering'}")
    else:
        print("usage: python -m support.stack --status | --stop")
