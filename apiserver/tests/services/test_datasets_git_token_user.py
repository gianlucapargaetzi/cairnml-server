"""datasets.git_token: a person's machine gets a token for a dataset repository at the level the person holds on it.

A read level gets a read-only token. A read_write level gets a token with write access to that one repository, from the
GitHub App. A person with no share gets the same answer as an invisible dataset. A job or an agent gets no token.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest

from apiserver.apierrors import errors
from apiserver.idp.github import GitHubError, RepoNotAccessible
from apiserver.services import datasets

REPO_URL = "https://github.com/acme/cars"
READ = {"contents": "read"}
WRITE = {"contents": "write"}


class FakeApp:
    """The GitHub App: records each token request, and can be told that the App is not installed or refuses."""

    def __init__(self, refuse_write=None):
        self.requests = []
        self.refuse_write = refuse_write

    def installation_token(self, repo, permissions=None):
        permissions = permissions or READ
        self.requests.append((repo, permissions))
        if permissions == WRITE and self.refuse_write is not None:
            raise self.refuse_write
        return SimpleNamespace(token=f"tok-{permissions['contents']}", expires_at=datetime(2030, 1, 1))


def person(token_kind="client", user="u1"):
    """The call a person's machine makes. The level the person holds is set by each test."""
    call = SimpleNamespace(
        data={},
        identity=SimpleNamespace(user=user, role="user"),
        auth=SimpleNamespace(token_kind=token_kind, job_task=None),
        real_ip="127.0.0.1",
        get_header=lambda header, default=None: default,
        HEADER_REAL_IP="X-Real-IP",
    )
    return call


@pytest.fixture
def run(monkeypatch):
    """Runs the endpoint for one person, with the App given and the dataset's repository not public. Returns the answer
    or the error, and the audit records made."""
    audits = []
    monkeypatch.setattr(datasets, "audit", lambda actor, action, **detail: audits.append((actor, action, detail)))
    monkeypatch.setattr(datasets, "client_address", lambda call: "127.0.0.1")
    monkeypatch.setattr(datasets, "public_repo", lambda repo: False)

    def run_as(call, repository=REPO_URL, level="read", app=None, held_level=None):
        call.data = {"repository": repository, "level": level}
        github = app if app is not None else FakeApp()
        monkeypatch.setattr(datasets, "build_from_config", lambda: github)
        monkeypatch.setattr(datasets.access_bll, "held_on_repository",
                            lambda company, user, is_admin, repo: (held_level, "dataset"))
        try:
            return datasets.git_token(call, "c1", None), github, audits
        except Exception as exc:  # the tests read the error themselves
            return exc, github, audits

    return run_as


def test_a_read_user_gets_a_read_only_token_for_the_dataset_repository(run):
    answer, github, _ = run(person(), level="read", held_level="read")

    assert answer["token"] == "tok-read"
    assert answer["username"] == "x-access-token"
    assert github.requests == [("acme/cars", READ)]


def test_a_read_user_gets_no_write_token(run):
    answer, github, _ = run(person(), level="read_write", held_level="read")

    assert isinstance(answer, errors.forbidden.NoWritePermission)
    assert all(permissions != WRITE for _repo, permissions in github.requests)


def test_a_read_write_user_gets_a_write_token_for_that_repository_only(run):
    answer, github, _ = run(person(), level="read_write", held_level="read_write")

    assert answer["token"] == "tok-write"
    assert github.requests == [("acme/cars", WRITE)]


def test_a_read_write_user_reading_gets_a_read_only_token(run):
    answer, github, _ = run(person(), level="read", held_level="read_write")

    assert answer["token"] == "tok-read"
    assert github.requests == [("acme/cars", READ)]


def test_a_user_with_no_share_is_refused_as_not_found(run):
    answer, github, _ = run(person(), level="read", held_level=None)

    assert isinstance(answer, errors.bad_request.ValidationError)
    assert "no such dataset" in answer.msg
    assert github.requests == []


def test_a_user_with_no_share_is_refused_for_a_write_as_not_found(run):
    answer, github, _ = run(person(), level="read_write", held_level=None)

    assert isinstance(answer, errors.bad_request.ValidationError)
    assert "no such dataset" in answer.msg
    assert github.requests == []


@pytest.mark.parametrize("kind", ["job", "agent"])
def test_a_job_or_an_agent_is_refused_before_the_dataset_is_looked_up(monkeypatch, kind):
    def must_not_be_asked(*args):
        raise AssertionError("the share of a job or an agent is never looked up")

    monkeypatch.setattr(datasets.access_bll, "held_on_repository", must_not_be_asked)
    monkeypatch.setattr(datasets, "audit", lambda *args, **kwargs: None)
    call = person(token_kind=kind)
    call.data = {"repository": REPO_URL, "level": "read_write"}

    with pytest.raises(errors.forbidden.NoWritePermission):
        datasets.git_token(call, "c1", None)


def test_a_write_with_no_app_installation_is_refused_with_the_reason(run):
    app = FakeApp(refuse_write=RepoNotAccessible("acme/cars"))
    answer, github, _ = run(person(), level="read_write", held_level="read_write", app=app)

    assert isinstance(answer, errors.bad_request.ValidationError)
    assert "the GitHub App is not installed on acme/cars" in answer.msg
    assert "write" in answer.msg


def test_a_write_the_app_is_not_allowed_to_grant_is_refused_with_the_reason(run):
    app = FakeApp(refuse_write=GitHubError("token request failed: 422"))
    answer, github, _ = run(person(), level="read_write", held_level="read_write", app=app)

    assert isinstance(answer, errors.bad_request.ValidationError)
    assert "token request failed: 422" in answer.msg


def test_the_token_is_never_written_to_the_audit_log(run):
    answer, _, audits = run(person(), level="read_write", held_level="read_write")

    assert answer["token"] == "tok-write"
    assert audits and all("tok-" not in str(entry) for entry in audits)
    assert [action for _actor, action, _detail in audits] == ["git_token.issued"]


def test_an_unknown_level_is_refused(run):
    answer, github, _ = run(person(), level="admin", held_level="read")

    assert isinstance(answer, errors.bad_request.ValidationError)
    assert github.requests == []
