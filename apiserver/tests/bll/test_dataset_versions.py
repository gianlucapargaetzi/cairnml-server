"""How the content of a dataset changed from commit to commit, read from its manifest."""

import json
import subprocess

import pytest

from apiserver.bll.dataset_versions import versions_of
from apiserver.idp import repo_visibility
from apiserver.idp.git_reader import GitReader


def _git(folder, *args):
    subprocess.run(["git", "-C", str(folder), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True)


def _sha(folder):
    return subprocess.run(["git", "-C", str(folder), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def dataset(tmp_path, monkeypatch):
    """A dataset repository with three commits: 100 samples, 250 samples with splits, and one without a manifest."""
    work, bare = tmp_path / "work", tmp_path / "remote" / "owner" / "data.git"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    commits = []
    for message, manifest in (("first", {"samples": 100}), ("more", {"samples": 250, "splits": {"train": 200, "test": 50}}), ("no manifest", None)):
        manifest_file = work / "manifest.json"
        if manifest is None:
            manifest_file.unlink()
        else:
            manifest_file.write_text(json.dumps(manifest))
        (work / "note.txt").write_text(message)
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", message)
        commits.append({"sha": _sha(work), "message": message, "date": f"2026-11-0{len(commits) + 1}T00:00:00+00:00"})
    bare.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", f"file://{work}", str(bare)], check=True)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(f'[url "file://{tmp_path / "remote"}/"]\n\tinsteadOf = https://example.test/\n')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(repo_visibility, "check_address", lambda name, port: "127.0.0.1")
    return "example.test", commits


def test_the_samples_and_splits_of_every_commit_oldest_first(dataset):
    host, commits = dataset
    with GitReader(host) as reader:
        result = versions_of(reader, "owner/data", list(reversed(commits)))
    assert [v["samples"] for v in result] == [100, 250, None]
    assert result[1]["splits"] == {"train": 200, "test": 50}
    assert result[2]["splits"] == {}
    assert [v["message"] for v in result] == ["first", "more", "no manifest"]
    assert result[0]["commit"] == commits[0]["sha"]


def test_a_commit_that_git_does_not_have_is_left_out_not_an_error(dataset):
    host, commits = dataset
    ghost = {"sha": "f" * 40, "message": "gone", "date": "2026-11-09T00:00:00+00:00"}
    with GitReader(host) as reader:
        assert [v["commit"] for v in versions_of(reader, "owner/data", [ghost] + list(reversed(commits)))] == [c["sha"] for c in commits]


def test_a_manifest_that_is_not_json_counts_as_no_manifest(dataset, tmp_path):
    host, commits = dataset

    class Reader:
        def commit_exists(self, repo, sha):
            return True

        def read_file(self, repo, path, ref=None):
            return b"{not json"

    assert versions_of(Reader(), "owner/data", commits)[0]["samples"] is None
