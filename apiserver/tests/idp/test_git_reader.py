import subprocess

import pytest

from apiserver.idp import git_reader, repo_visibility
from apiserver.idp.git_reader import GitReader


def _git(folder, *args):
    subprocess.run(["git", "-C", str(folder), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
                   capture_output=True)


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A host whose repositories are local folders: git's own url rewriting stands in for the network."""
    work, bare = tmp_path / "work", tmp_path / "remote" / "owner" / "registry.git"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    (work / "datasets").mkdir()
    (work / "datasets" / "cifar10.yaml").write_text("source: hf\nlink: https://example.test/cifar\n")
    (work / "datasets" / "notes.bin").write_bytes(b"\xff\xfe")
    (work / "DATASET.md").write_text("hello")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "first")
    (work / "DATASET.md").write_text("second")
    _git(work, "commit", "-qam", "second")
    bare.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", f"file://{work}", str(bare)], check=True)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(f'[url "file://{tmp_path / "remote"}/"]\n\tinsteadOf = https://example.test/\n')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(repo_visibility, "check_address", lambda name, port: "127.0.0.1")
    return "example.test"


def test_reads_a_folder_and_a_file_over_git(host):
    with GitReader(host) as reader:
        assert reader.read_dir_texts("owner/registry", "datasets") == {"cifar10.yaml": "source: hf\nlink: https://example.test/cifar\n"}
        assert reader.read_file("owner/registry", "DATASET.md") == b"second"
        assert reader.read_file("owner/registry", "missing.txt") is None
        assert reader.read_dir_texts("owner/registry", "nothing") == {}


def test_does_not_read_outside_the_clone(host):
    with GitReader(host) as reader:
        assert reader.read_file("owner/registry", "../../../etc/passwd") is None


def test_head_history_and_an_older_revision(host):
    with GitReader(host) as reader:
        branch, head = reader.default_branch_head("owner/registry")
        commits = reader.list_commits("owner/registry", branch, 10)
        assert branch == "main" and [c["message"] for c in commits] == ["second", "first"] and commits[0]["sha"] == head
        assert reader.commit_exists("owner/registry", commits[1]["sha"]) and not reader.commit_exists("owner/registry", "0" * 40)
        assert reader.read_file("owner/registry", "DATASET.md", commits[1]["sha"]) == b"hello"


def test_an_unknown_repository_is_not_accessible(host):
    from apiserver.idp.github import RepoNotAccessible
    with GitReader(host) as reader, pytest.raises(RepoNotAccessible):
        reader.read_file("owner/nothing", "x")
    assert not GitReader(host).repository_visible("owner/nothing")


def test_the_token_is_only_in_the_environment_of_git():
    env = GitReader("example.test", "secret-token", "x-access-token")._env()
    assert "secret-token" not in " ".join(env.values()) and env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert git_reader._first_line("fatal: bad secret-token here", "secret-token") == "fatal: bad *** here"


def test_the_size_of_every_file_is_listed_for_a_commit(host):
    with GitReader(host) as reader:
        _branch, head = reader.default_branch_head("owner/registry")
        sizes = reader.tree_sizes("owner/registry", head)
        assert sizes["DATASET.md"] == len("second") and sizes["datasets/cifar10.yaml"] > 0 and "datasets" not in sizes


def test_a_repository_without_dataset_yaml_is_not_a_dataset_when_read_over_git(host):
    from apiserver.bll.dataset_layout import NotADataset, verify_dataset
    with GitReader(host) as reader, pytest.raises(NotADataset, match="dataset.yaml is missing"):
        verify_dataset(reader, "owner/registry")


def test_a_repository_that_cannot_be_cloned_is_refused_with_the_reason(host):
    from apiserver.apierrors import errors
    from apiserver.bll.dataset_layout import verify_dataset
    with GitReader(host) as reader, pytest.raises(errors.bad_request.ValidationError, match="could not be read"):
        verify_dataset(reader, "owner/nothing")


def test_the_history_gives_the_count_the_first_commit_and_the_newest_commits(host):
    with GitReader(host) as reader:
        info = reader.commit_history("owner/registry", 30)
    assert info["total"] == 2
    assert info["first"]["message"] == "first"
    assert [c["message"] for c in info["commits"]] == ["second", "first"]
    assert info["commits"][0]["parents"] == [info["first"]["sha"]]


def test_the_history_limit_keeps_the_newest_commits(host):
    with GitReader(host) as reader:
        info = reader.commit_history("owner/registry", 1)
    assert info["total"] == 2
    assert [c["message"] for c in info["commits"]] == ["second"]
