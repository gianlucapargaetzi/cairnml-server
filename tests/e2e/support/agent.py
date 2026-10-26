"""A machine that runs jobs: its own HOME and device key, and the agent daemon running as a process, listening to the
default queue."""

import os
import signal
import subprocess
import time
from pathlib import Path

from . import cli, settings
from .client import agent_device_id_of, agent_key_path

# How often the agent polls its queue on the test stack, in seconds: a job is picked up within seconds, not minutes.
POLLING_INTERVAL_SEC = 2


def shared_cache_settings() -> dict:
    """In the isolated environment (tests/e2e/docker) the agent's job containers are started by the nested daemon, which
    can only mount folders on the shared volume, at the same path as here. So the agent's caches and build folders are
    moved there. On the host (no CAIRNML_E2E_SHARED_ROOT) the agent keeps its defaults."""
    shared = os.environ.get("CAIRNML_E2E_SHARED_ROOT")
    if not shared:
        return {}
    cache = Path(shared) / "agent-cache"
    return {
        "CAIRNML_AGENT__agent__venvs_dir": str(cache / "venvs-builds"),
        "CAIRNML_AGENT__agent__venvs_cache__path": str(cache / "venvs-cache"),
        "CAIRNML_AGENT__agent__vcs_cache__path": str(cache / "vcs-cache"),
        "CAIRNML_AGENT__agent__pip_download_cache__path": str(cache / "pip-download-cache"),
        "CAIRNML_AGENT__agent__docker_pip_cache": str(cache / "pip-cache"),
        "CAIRNML_AGENT__agent__docker_apt_cache": str(cache / "apt-cache"),
    }


# The worker lookup and the agent helpers below rely on the server's worker list:
# about 6 s, with a generous margin.
LOST_WITHIN_SEC = 30


def init(home: Path, account, *, server: str = None, name: str = "e2e-agent"):
    """cairnml-agent init --server, signed in as account (user, password) through the sign-in page. Only an admin may
    enrol an agent."""
    user, password = account
    args = ["init", "--server", server or settings.API_URL, "--name", name]
    return cli.run("cairnml-agent", home, login_user=user, login_password=password, args=args, timeout=300)


class AgentMachine:
    def __init__(self, home: Path, worker_name: str):
        self.home = home
        self.worker_name = worker_name
        self.log_path = home / "daemon.log"
        self._proc = None

    @property
    def device_id(self) -> str:
        return agent_device_id_of(self.home)

    @property
    def key_path(self) -> Path:
        return agent_key_path(self.home)

    def start(self) -> None:
        env = cli.environment(self.home)
        env["CAIRNML_WORKER_NAME"] = self.worker_name
        # the test machine has no GPU: the agent adds no --gpus flag (its own image sets the same, for a CPU host)
        env["CAIRNML_DOCKER_SKIP_GPUS_FLAG"] = "1"
        env.update(shared_cache_settings())
        log = open(self.log_path, "ab")
        self._proc = subprocess.Popen(
            [str(cli.VENV_BIN / "cairnml-agent"), "daemon", "--queue", "default", "--docker", "python:3.12-slim",
             "--polling-interval", str(POLLING_INTERVAL_SEC)],
            env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self, timeout: float = 15) -> None:
        if not self.running:
            return
        os.killpg(self._proc.pid, signal.SIGTERM)
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(self._proc.pid, signal.SIGKILL)
            self._proc.wait(timeout=10)

    def log(self) -> str:
        return self.log_path.read_text(errors="replace") if self.log_path.exists() else ""

    def wait_until(self, condition, timeout: float = 60, poll: float = 1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = condition()
            if value:
                return value
            time.sleep(poll)
        return None


def worker_record(api, worker_name: str):
    """This worker as the server lists it, or None."""
    workers = api.authed("workers.get_all", {})
    # the server names a worker "<name>:<n>", where <n> counts the daemons of that name
    return next((w for w in workers.get("workers", [])
                 if w.get("id") == worker_name or str(w.get("id", "")).startswith(worker_name + ":")), None)
