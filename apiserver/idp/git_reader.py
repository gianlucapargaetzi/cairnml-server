"""Reads files of a git repository over git itself (smart HTTP), never through a host's REST API.

A shallow clone of the default branch into a temporary folder, then plain file reads. The host's REST quota (60 an hour for an
anonymous caller on GitHub) is not touched. A token, when there is one, goes to git in the environment as an Authorization
header, never in the address or on the command line, so it shows in no process list and no log.
"""

import base64
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from apiserver.idp import repo_visibility
from apiserver.idp.github import GitHubError, RepoNotAccessible

CLONE_TIMEOUT = 60
MAX_FILE = 1_000_000
HISTORY = 60  # commits kept, for the history a dataset shows


class GitReader:
    """``read_file`` and ``read_dir_texts`` for one repository (``host`` and ``path``), as the REST readers offer them, so the
    registry reading is the same whichever reads it. The clone is made once and kept until ``close``."""

    def __init__(self, host: str, token: Optional[str] = None, username: Optional[str] = None) -> None:
        self.host, self.token, self.username = host, token, username or "x-access-token"
        self._dir: Optional[str] = None
        self._path: Optional[str] = None

    def __enter__(self) -> "GitReader":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
        self._dir = self._path = None

    def _env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR")}
        env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_ASKPASS": "true"})
        if self.token:
            basic = base64.b64encode(f"{self.username}:{self.token}".encode()).decode()
            env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
                        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"})
        return env

    def _clone(self, repo: str) -> str:
        if self._dir and self._path == repo:
            return self._dir
        self.close()
        try:
            repo_visibility.check_address(self.host, 443)
        except repo_visibility.ProbeBlocked as exc:
            raise GitHubError(str(exc)) from None
        except repo_visibility.ProbeFailed:
            raise GitHubError(f"{self.host} could not be reached") from None
        folder = tempfile.mkdtemp(prefix="cairnml-git-")
        try:
            done = subprocess.run(
                ["git", "clone", "--quiet", "--depth", str(HISTORY), "--single-branch", "--no-tags", "--",
                 f"https://{self.host}/{repo}.git", folder],
                env=self._env(), capture_output=True, text=True, timeout=CLONE_TIMEOUT, check=False)
        except subprocess.TimeoutExpired:
            shutil.rmtree(folder, ignore_errors=True)
            raise GitHubError(f"{repo} could not be read in time") from None
        if done.returncode != 0:
            shutil.rmtree(folder, ignore_errors=True)
            raise RepoNotAccessible(f"{repo} could not be read over git: " + _first_line(done.stderr, self.token))
        self._dir, self._path = folder, repo
        return folder

    def _git(self, repo: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", self._clone(repo), *args], env=self._env(), capture_output=True,
                              timeout=CLONE_TIMEOUT, check=False)

    def default_branch_head(self, repo: str) -> tuple:
        """(default branch, the sha of its latest commit). An empty repository has no head: refused."""
        branch = self._git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        head = self._git(repo, "rev-parse", "--verify", "HEAD")
        if branch.returncode or head.returncode:
            raise GitHubError(f"{repo} has no commit yet")
        return branch.stdout.decode().strip(), head.stdout.decode().strip()

    def repository_visible(self, repo: str) -> bool:
        """Whether the repository could be cloned at all (an empty one can)."""
        try:
            self._clone(repo)
        except GitHubError:
            return False
        return True

    def tree_sizes(self, repo: str, ref: str) -> dict:
        """The size in bytes of every file in the repository at a commit, by path."""
        if not _is_sha(ref) or not self.commit_exists(repo, ref):
            raise GitHubError(f"no commit {ref[:10]} in {repo}")
        done = self._git(repo, "ls-tree", "-r", "-l", "-z", ref)
        if done.returncode:
            raise GitHubError(f"tree lookup failed for {repo}")
        sizes = {}
        for record in done.stdout.decode("utf-8", "replace").split("\0"):
            meta, _, path = record.partition("\t")
            fields = meta.split()
            if path and len(fields) == 4 and fields[1] == "blob":
                sizes[path] = int(fields[3]) if fields[3].isdigit() else 0
        return sizes

    def commit_exists(self, repo: str, sha: str) -> bool:
        if not _is_sha(sha):
            return False
        if self._git(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0:
            return True
        fetched = self._git(repo, "fetch", "--quiet", "--depth", "1", "origin", sha)
        return fetched.returncode == 0 and self._git(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0

    def list_commits(self, repo: str, ref: str, limit: int) -> list:
        """The latest commits of the default branch, newest first: sha, first line of the message, author, date, parent shas."""
        done = self._git(repo, "log", f"--max-count={min(limit, HISTORY)}", "--format=%H%x1f%s%x1f%an%x1f%aI%x1f%P%x1e", "HEAD")
        if done.returncode:
            raise GitHubError(f"commit list failed for {repo}")
        commits = []
        for record in done.stdout.decode("utf-8", "replace").split("\x1e"):
            fields = record.strip("\n").split("\x1f")
            if len(fields) == 5:
                commits.append({"sha": fields[0], "message": fields[1], "author": fields[2], "date": fields[3],
                                "parents": fields[4].split()})
        return commits

    def read_file(self, repo: str, path: str, ref: Optional[str] = None) -> Optional[bytes]:
        if ref and _is_sha(ref) and self.commit_exists(repo, ref):
            done = self._git(repo, "show", f"{ref}:{path}")
            return done.stdout if done.returncode == 0 and len(done.stdout) <= MAX_FILE else None
        target = self._inside(repo, path)
        if target is None or not target.is_file() or target.stat().st_size > MAX_FILE:
            return None
        return target.read_bytes()

    def read_dir_texts(self, repo: str, path: str) -> dict:
        folder = self._inside(repo, path)
        texts = {}
        if folder is None or not folder.is_dir():
            return texts
        for item in sorted(folder.iterdir()):
            if item.is_file() and not item.is_symlink() and item.stat().st_size <= MAX_FILE:
                try:
                    texts[item.name] = item.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
        return texts

    def _inside(self, repo: str, path: str) -> Optional[Path]:
        root = Path(self._clone(repo)).resolve()
        target = (root / path).resolve()
        return target if root == target or root in target.parents else None


def _is_sha(text: str) -> bool:
    return 7 <= len(text or "") <= 40 and all(c in "0123456789abcdef" for c in text.lower())


def _first_line(text: str, token: Optional[str]) -> str:
    line = next((x for x in (text or "").splitlines() if x.strip()), "git failed")
    return line.replace(token, "***") if token else line
