"""repo_of reads the owner and name of a GitHub repository from every usual form of its address."""

import pytest

from apiserver.idp.github_check import repo_of


@pytest.mark.parametrize("address", [
    "git@github.com:gianlucapargaetzi/cairnml-examples.git",
    "ssh://git@github.com/gianlucapargaetzi/cairnml-examples.git",
    "https://github.com/gianlucapargaetzi/cairnml-examples.git",
    "https://github.com/gianlucapargaetzi/cairnml-examples",
    "http://github.com/gianlucapargaetzi/cairnml-examples",
    "github.com/gianlucapargaetzi/cairnml-examples",
])
def test_every_form_of_a_github_address_gives_the_same_repository(address):
    assert repo_of(address) == "gianlucapargaetzi/cairnml-examples"


def test_another_host_is_not_github():
    assert repo_of("git@gitlab.example.org:team/proj.git") is None
    assert repo_of("https://gitlab.example.org/team/proj") is None
