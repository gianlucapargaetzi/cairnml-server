import http.server
import socket
import subprocess
import threading
from types import SimpleNamespace

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


def test_the_graph_has_the_count_the_first_commit_and_the_newest_commits(host):
    with GitReader(host) as reader:
        info = reader.graph_history("owner/registry", [], 30)
    assert info["total"] == 2
    assert info["first"]["message"] == "first"
    assert [c["message"] for c in info["commits"]] == ["second", "first"]
    assert info["commits"][0]["parents"] == [info["first"]["sha"]]
    assert list(info["tips"].values()) == [["main"]]


def test_the_graph_limit_keeps_the_newest_commits(host):
    with GitReader(host) as reader:
        info = reader.graph_history("owner/registry", [], 1)
    assert info["total"] == 2
    assert [c["message"] for c in info["commits"]] == ["second"]


def test_a_branch_with_a_task_of_its_own_is_part_of_the_graph_and_one_without_is_not(host, tmp_path):
    bare = tmp_path / "remote" / "owner" / "registry.git"
    work = tmp_path / "branches"
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True)
    _git(work, "checkout", "-q", "-b", "feature/with-task")
    (work / "a.txt").write_text("a")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "on the branch")
    on_branch = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    _git(work, "push", "-q", "origin", "feature/with-task")
    _git(work, "checkout", "-q", "-b", "feature/nothing-ran", "main")
    (work / "b.txt").write_text("b")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "no task here")
    _git(work, "push", "-q", "origin", "feature/nothing-ran")
    with GitReader(host) as reader:
        info = reader.graph_history("owner/registry", [on_branch, on_branch[:10]], 30)
    assert sorted(n for names in info["tips"].values() for n in names) == ["feature/with-task", "main"]
    assert info["total"] == 3
    assert [c["message"] for c in info["commits"]][0] == "on the branch"
    assert info["resolved"] == {on_branch: on_branch, on_branch[:10]: on_branch}


# --- The address guard reaches git: no redirect, and the checked address is the one connected to ---------------------------

REDIRECT_TARGET = "/internal/info/refs?service=git-upload-pack"


def _serve(handler):
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def redirecting_host(tmp_path, monkeypatch):
    """example.test is rewritten to loopback server A, which answers every request with a 302 to loopback server B. B stands
    for an address the guard refuses; it records every path it is asked for."""
    first, second = [], []

    class Second(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            second.append(self.path)
            self.send_response(404)
            self.end_headers()
        log_message = lambda *args: None

    b = _serve(Second)

    class First(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            first.append(self.path)
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{b.server_address[1]}{REDIRECT_TARGET}")
            self.end_headers()
        log_message = lambda *args: None

    a = _serve(First)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(f'[url "http://127.0.0.1:{a.server_address[1]}/"]\n\tinsteadOf = https://example.test/\n')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(repo_visibility, "check_address", lambda name, port: "127.0.0.1")
    yield SimpleNamespace(host="example.test", first=first, second=second)
    for server in (a, b):
        server.shutdown()
        server.server_close()


def test_a_redirect_is_not_followed_on_a_read(redirecting_host):
    from apiserver.idp.github import RepoNotAccessible
    with GitReader(redirecting_host.host) as reader, pytest.raises(RepoNotAccessible) as raised:
        reader.read_file("owner/repo", "DATASET.md")
    assert redirecting_host.second == []  # the redirect was not followed: nothing reached the second server
    assert redirecting_host.first and "info/refs" in redirecting_host.first[0]  # and the first server was asked, so git did run
    assert "302" in str(raised.value)


def test_a_redirect_is_not_followed_on_the_graph(redirecting_host):
    from apiserver.idp.github import RepoNotAccessible
    with pytest.raises(RepoNotAccessible) as raised:
        GitReader(redirecting_host.host).graph_history("owner/repo", [], 30)
    assert redirecting_host.second == []
    assert redirecting_host.first and "info/refs" in redirecting_host.first[0]
    assert "302" in str(raised.value)


def test_pinned_address_is_the_one_connected(tmp_path, monkeypatch):
    """git reaches the address that was given to the guard helper although the name does not resolve; without it, it cannot."""
    seen = []

    class Server(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(404)
            self.end_headers()
        log_message = lambda *args: None

    server = _serve(Server)
    port = server.server_address[1]
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    env = GitReader("pinned.invalid")._env()
    url = f"http://pinned.invalid:{port}/owner/repo.git"
    try:
        pinned = subprocess.run(["git", *git_reader._guard_args("pinned.invalid", "127.0.0.1", port=port), "clone", "--quiet", "--", url,
                                 str(tmp_path / "a")], env=env, capture_output=True, text=True, timeout=30)
        after_pinned = len(seen)
        unpinned = subprocess.run(["git", "clone", "--quiet", "--", url, str(tmp_path / "b")], env=env, capture_output=True, text=True,
                                  timeout=30)
    finally:
        server.shutdown()
        server.server_close()
    assert seen                       # the pinned clone reached the server although the name does not resolve
    assert len(seen) == after_pinned  # and the clone without the pin did not (the name has no address)
    assert pinned.returncode == unpinned.returncode == 128


def test_the_pin_for_an_ipv6_address_has_no_brackets():
    args = git_reader._guard_args("example.test", "2001:db8::1")
    assert "http.curloptResolve=example.test:443:2001:db8::1" in args and "http.followRedirects=false" in args


def _recording_run(monkeypatch):
    calls, real = [], subprocess.run

    def record(argv, *args, **kwargs):
        calls.append(list(argv))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(git_reader.subprocess, "run", record)
    return calls


def test_guard_args_on_every_call(host, monkeypatch):
    calls = _recording_run(monkeypatch)
    with GitReader(host) as reader:
        reader.read_file("owner/registry", "DATASET.md")
        reader.list_commits("owner/registry", "main", 5)
        reader.commit_exists("owner/registry", "1" * 40)
        assert reader.repository_visible("owner/registry")
    GitReader(host).graph_history("owner/registry", [], 30)
    network = [argv for argv in calls if argv[0] == "git" and "--version" not in argv]
    verbs = {argv[argv.index("clone") if "clone" in argv else argv.index("-C") + 2] for argv in network}
    assert {"clone", "fetch", "log"} <= verbs
    for argv in network:
        before_the_verb = argv[:argv.index("clone") if "clone" in argv else argv.index("-C")]
        for option in ("http.followRedirects=false", "http.curloptResolve=example.test:443:127.0.0.1"):
            assert ["-c", option] == [before_the_verb[i] for i in (before_the_verb.index(option) - 1, before_the_verb.index(option))], argv


def test_resolver_stub_pin(monkeypatch):
    """The guard resolves once; a later, different answer (127.0.0.1) never becomes the address git is pinned to."""
    resolved = []

    def getaddrinfo(name, port, type=0, **kwargs):
        resolved.append(name)
        address = "93.184.216.34" if len(resolved) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    argv_seen = []

    def fake_run(argv, *args, **kwargs):
        argv_seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 128, "", "fatal: stopped")

    monkeypatch.setattr(repo_visibility.socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(git_reader, "_git_version", lambda: (2, 43, 0))  # the stub of subprocess.run below must not answer it
    monkeypatch.setattr(git_reader.subprocess, "run", fake_run)
    from apiserver.idp.github import RepoNotAccessible
    with GitReader("example.test") as reader, pytest.raises(RepoNotAccessible):
        reader.read_file("owner/repo", "x")
    clones = [argv for argv in argv_seen if "clone" in argv]
    assert len(clones) == 1 and len(resolved) == 1
    assert "http.curloptResolve=example.test:443:93.184.216.34" in clones[0]
    assert not any("127.0.0.1" in part for part in clones[0])


def test_versions_are_compared_as_numbers():
    assert (2, 100, 0) >= git_reader.MIN_GIT > (2, 36, 1)  # (2, 100) is newer than (2, 37); a string compare says the opposite


@pytest.mark.parametrize("text, version", [
    ("git version 2.36.1", (2, 36, 1)),
    ("git version 2.43.0", (2, 43, 0)),
    ("git version 2.100.0 (Apple Git-1)", (2, 100, 0)),
    ("git version 3.0.0", (3, 0, 0)),
])
def test_parse_git_version(text, version):
    assert git_reader._parse_git_version(text) == version


def test_an_unreadable_git_version_is_refused(monkeypatch):
    from apiserver.idp.github import GitHubError
    assert git_reader._parse_git_version("something else") == (0,)
    monkeypatch.setattr(git_reader, "_git_version", lambda: (0,))
    with pytest.raises(GitHubError, match="2.37"):
        git_reader._check_git_version()


def test_git_too_old_is_refused(host, monkeypatch):
    from apiserver.idp.github import GitHubError
    monkeypatch.setattr(git_reader, "_git_version", lambda: (2, 36, 1))
    with GitReader(host) as reader, pytest.raises(GitHubError, match="2.37"):
        reader.read_file("owner/registry", "DATASET.md")
    with pytest.raises(GitHubError, match="2.37"):
        GitReader(host).graph_history("owner/registry", [], 30)


def test_a_failed_git_version_lookup_is_not_kept(monkeypatch):
    answers = iter(["", "git version 2.43.0"])
    monkeypatch.setattr(git_reader, "_git_version_read", None)
    monkeypatch.setattr(git_reader.subprocess, "run",
                        lambda argv, *args, **kwargs: subprocess.CompletedProcess(argv, 0, next(answers), ""))
    assert git_reader._git_version() == (0,)          # nothing readable: refused, but not remembered
    assert git_reader._git_version() == (2, 43, 0)    # the next lookup works, and is kept
    assert git_reader._git_version() == (2, 43, 0)    # a third call does not run git again (the iterator is spent)


def _ipv6_loopback_available():
    try:
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        probe.bind(("::1", 0))
        probe.close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _ipv6_loopback_available(), reason="no IPv6 loopback here")
def test_pinned_ipv6_address_is_connected_by_real_git(tmp_path, monkeypatch):
    seen = []

    class Server(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(404)
            self.end_headers()
        log_message = lambda *args: None

    class Server6(http.server.HTTPServer):
        address_family = socket.AF_INET6

    server = Server6(("::1", 0), Server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    try:
        subprocess.run(["git", *git_reader._guard_args("pinned.invalid", "::1", port=port), "clone", "--quiet", "--",
                        f"http://pinned.invalid:{port}/owner/repo.git", str(tmp_path / "c")], env=GitReader("pinned.invalid")._env(),
                       capture_output=True, text=True, timeout=30)
    finally:
        server.shutdown()
        server.server_close()
    assert seen
