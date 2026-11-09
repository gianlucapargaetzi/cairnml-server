"""A reader sees a repository when the repository lookup answers 200, empty or not: an empty repository has no branch, so
the branch lookup is not a test of access."""
from types import SimpleNamespace

import pytest

from apiserver.idp.github import GitHubApp, GitHubError


class Reader(GitHubApp):
    """A reader that sends one fixed header, so the lookup itself is what the tests exercise."""

    def __init__(self, api_url):
        super().__init__("0", "", api_url)

    def _read_headers(self, repo):
        return self._headers("token")


def reader_answering(monkeypatch, status):
    calls = []

    def send(self, method, url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=status, json=lambda: {})

    monkeypatch.setattr(Reader, "_send", send)
    return Reader("https://api.github.com"), calls


def test_an_empty_repository_is_visible_when_its_lookup_answers(monkeypatch):
    reader, calls = reader_answering(monkeypatch, 200)
    assert reader.repository_visible("acme/empty") is True
    assert calls == ["https://api.github.com/repos/acme/empty"]


@pytest.mark.parametrize("status", [403, 404])
def test_a_repository_that_github_hides_is_not_visible(monkeypatch, status):
    reader, _calls = reader_answering(monkeypatch, status)
    assert reader.repository_visible("acme/private") is False


def test_a_github_failure_is_raised_not_read_as_no_access(monkeypatch):
    reader, _calls = reader_answering(monkeypatch, 500)
    with pytest.raises(GitHubError):
        reader.repository_visible("acme/private")
