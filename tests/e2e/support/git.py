"""Local git repositories for the git tests: a bare repository stands in for the remote, and a clone of it is the
checkout a person works in. No network and no GitHub are involved."""

import os
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

IDENTITY = {
    "GIT_AUTHOR_NAME": "E2E Test", "GIT_AUTHOR_EMAIL": "e2e@example.test",
    "GIT_COMMITTER_NAME": "E2E Test", "GIT_COMMITTER_EMAIL": "e2e@example.test",
}


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                         env={**os.environ, **IDENTITY}, timeout=120)
    assert out.returncode == 0, f"git {' '.join(args)} failed: {out.stderr}"
    return out.stdout.strip()


def make_remote_and_checkout(root: Path, branch: str = "main"):
    """A bare remote with one commit on the branch, and a clone of it. Returns (remote path, checkout path)."""
    remote = root / "remote.git"
    checkout = root / "checkout"
    remote.mkdir(parents=True)
    git(remote, "init", "--bare", "-b", branch)
    seed = root / "seed"
    seed.mkdir()
    git(seed, "init", "-b", branch)
    (seed / "README.md").write_text("e2e repository\n")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "initial commit")
    git(seed, "remote", "add", "origin", remote.as_posix())
    git(seed, "push", "origin", branch)
    git(root, "clone", remote.as_posix(), checkout.as_posix())
    return remote, checkout


def commit_all(checkout: Path, message: str) -> str:
    git(checkout, "add", "-A")
    git(checkout, "commit", "-m", message)
    return git(checkout, "rev-parse", "HEAD")


@contextmanager
def served(root: Path):
    """The repositories under root, over git:// on the loopback address, while the block runs. A job's container clones
    them as it would clone a remote: a local path is not visible inside the container, but this address is, because a
    job container on a loopback server runs with the host's network (cairnml-agent, worker.py). Yields the base URL."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    daemon = subprocess.Popen(
        ["git", "daemon", "--export-all", f"--base-path={root.as_posix()}", "--listen=127.0.0.1", f"--port={port}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while True:
            with socket.socket() as attempt:
                if attempt.connect_ex(("127.0.0.1", port)) == 0:
                    break
            if daemon.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(f"git daemon did not start: {daemon.stderr.read()}")
            time.sleep(0.1)
        yield f"git://127.0.0.1:{port}"
    finally:
        daemon.terminate()
        daemon.wait(timeout=10)
