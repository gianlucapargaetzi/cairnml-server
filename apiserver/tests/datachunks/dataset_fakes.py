"""Fakes for the dataset tests: a GitHub App client that holds some repositories, and the files of a valid dataset.

No test that uses these reaches the network.
"""

import json
from datetime import datetime
from types import SimpleNamespace

from apiserver.idp.github import GitHubError, RepoNotAccessible
from cairnml.datasets import footprint as fp

DVC = "outs:\n- md5: 465a62c55506cd2fa10372c1f9ba4b5e.dir\n  size: 2048\n  nfiles: 3\n  hash: md5\n  path: data\n"
HEAD = "a" * 40
ADAPTER = b"class Adapter:\n    pass\n"


def good_files(dvc=DVC, labels=None):
    lock = fp.build_lock(fp.parse_dvc_file(DVC), {"kind": "native"}, 3, {"image": "image"},
                         labels or {"class": "class"}, "2.1.13")
    return {fp.DVC_FILE: dvc.encode(), fp.CONFIG_FILE: b"format: native\n", fp.LOCK_FILE: json.dumps(lock).encode()}


def local_files(adapter=ADAPTER, lock_adapter=ADAPTER):
    """A local dataset: dataset.yaml says adapter: local, and the lock was checked with ``lock_adapter``."""
    fmt = {"kind": "local", "name": "local", "adapter_sha256": fp.sha256_bytes(lock_adapter)}
    lock = fp.build_lock(fp.parse_dvc_file(DVC), fmt, 3, {"image": "image"}, {"class": "class"}, "2.1.13")
    files = {fp.DVC_FILE: DVC.encode(), fp.CONFIG_FILE: b"adapter: local\n", fp.LOCK_FILE: json.dumps(lock).encode()}
    if adapter is not None:
        files[fp.ADAPTER_FILE] = adapter
    return files


class FakeGitHub:
    """The GitHub App's client for the tests: the repositories it is installed on, each with its files at the default
    branch (a path maps to its bytes, or to None when the file is missing)."""

    def __init__(self, repos=None):
        self.repos = {name: dict(files) for name, files in (repos or {}).items()}
        self.reads = []  # (repository, path, ref) of every file read
        self.token_requests = []  # repository of every installation token asked for
        self.tree_overrides = {}  # sizes the tree lists on top of the files
        self.failing = set()  # repositories whose reads fail, as GitHub does when it is down

    def installed_repositories(self):
        return [{"repository": name, "default_branch": "main", "private": True} for name in self.repos]

    def default_branch_head(self, repo):
        if repo not in self.repos:
            raise RepoNotAccessible(repo)
        if repo in self.failing:
            raise GitHubError("repository lookup failed: 502")
        return "main", HEAD

    def read_file(self, repo, path, ref=None):
        self.reads.append((repo, path, ref))
        if repo in self.failing:
            raise GitHubError("file read failed: 502")
        return self.repos[repo].get(path)

    def tree_sizes(self, repo, ref):
        sizes = {path: len(content) for path, content in self.repos[repo].items() if content is not None}
        sizes.update(self.tree_overrides)
        return sizes

    def installation_token(self, repo, permissions=None):
        self.token_requests.append(repo)
        if repo not in self.repos:
            raise RepoNotAccessible(repo)
        return SimpleNamespace(token=f"token-for-{repo}", expires_at=datetime(2030, 1, 1))

    def commit_exists(self, repo, sha):
        return True
