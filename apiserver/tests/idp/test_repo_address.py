"""One repository has one key, whatever the spelling of its address: the scheme, a user, a port, ``www.``, a trailing
``.git`` or slash, and the case on the hosts that ignore it. What is not a hosted repository has none."""
import pytest

from apiserver.idp.repo_address import key_of, parse

GITHUB = "github.com/acme/proj"


@pytest.mark.parametrize("address", [
    "https://github.com/acme/proj",
    "https://github.com/acme/proj.git",
    "https://github.com/acme/proj/",
    "https://github.com/acme/proj.git/",
    "http://github.com/acme/proj",
    "git://github.com/acme/proj.git",
    "git@github.com:acme/proj.git",
    "git@github.com:acme/proj",
    "ssh://git@github.com/acme/proj.git",
    "ssh://git@github.com:22/acme/proj.git",
    "git+ssh://git@github.com/acme/proj",
    "https://user:token@github.com/acme/proj",
    "https://x-access-token:abc@github.com/acme/proj.git",
    "https://www.github.com/acme/proj",
    "github.com/acme/proj",
    "HTTPS://GITHUB.COM/Acme/Proj.GIT",
    "https://github.com/ACME/PROJ?tab=readme",
    "https://github.com/acme/proj#readme",
    "  https://github.com/acme/proj  ",
    "https://github.com//acme//proj",
])
def test_every_spelling_of_a_github_repository_has_one_key(address):
    assert key_of(address) == GITHUB


def test_the_clone_address_is_https_and_keeps_the_first_case():
    assert parse("git@github.com:Acme/Proj.git").url == "https://github.com/Acme/Proj"
    assert parse("git@github.com:Acme/Proj.git").key == GITHUB


@pytest.mark.parametrize("address, key", [
    ("https://gitlab.com/Group/Sub/Project.git", "gitlab.com/group/sub/project"),
    ("git@gitlab.com:group/sub/project", "gitlab.com/group/sub/project"),
    ("https://bitbucket.org/Team/Repo", "bitbucket.org/team/repo"),
    ("ssh://git@git.example.com:2222/Team/Repo.git", "git.example.com/Team/Repo"),
    ("https://git.example.com/Team/Repo", "git.example.com/Team/Repo"),
])
def test_other_hosts_have_a_key_too(address, key):
    assert key_of(address) == key


def test_a_host_that_keeps_case_does_not_merge_names_that_differ_in_case():
    assert key_of("https://git.example.com/Team/Repo") != key_of("https://git.example.com/team/repo")


def test_two_hosts_are_two_repositories():
    assert key_of("https://github.com/acme/proj") != key_of("https://gitlab.com/acme/proj")


@pytest.mark.parametrize("address", [
    "", None, "owner/name", "name", "/home/me/proj", "C:/Users/me/proj", "C:\\Users\\me\\proj", "file:///tmp/proj",
    "ftp://example.com/acme/proj", "https://github.com/acme", "https://github.com/", "https://github.com",
    "https://github.com/acme/proj/tree/main", "https://github.com/acme/proj/issues/3", "localhost:acme/proj",
    "https://localhost/acme/proj", "git@github.com:proj", "https://[::1]/acme/proj",
])
def test_what_is_not_a_hosted_repository_has_no_key(address):
    assert key_of(address) is None
