from datetime import datetime

import pytest
from mongoengine import ValidationError

from apiserver.database.model.share import Share


def make(**overrides) -> Share:
    values = dict(id="s1", company="c1", resource_type="queue", resource_id="q1", scope="projects", project="p1",
                  level="read", granted_by="u1", created=datetime.utcnow())
    values.update(overrides)
    return Share(**values)


def test_a_project_share_is_valid_with_read_and_inherited_defaults_to_false():
    share = make()
    share.validate()
    assert share.inherited is False
    assert share.user == ""


def test_a_public_share_with_a_project_is_refused():
    with pytest.raises(ValidationError):
        make(scope="public", project="p1", level="read").validate()


def test_a_public_share_without_a_project_is_valid():
    make(scope="public", project="", level="read").validate()


@pytest.mark.parametrize("level", ["read_write", "collaborator"])
def test_a_public_share_allows_only_read(level):
    with pytest.raises(ValidationError):
        make(scope="public", project="", level=level).validate()


def test_a_read_write_share_on_a_registry_is_refused():
    with pytest.raises(ValidationError):
        make(resource_type="registry", level="read_write").validate()


def test_a_read_write_share_on_a_credential_is_refused():
    with pytest.raises(ValidationError):
        make(resource_type="credential", level="read_write").validate()


@pytest.mark.parametrize("resource_type, level", [
    ("queue", "read"),
    ("queue", "read_write"),
    ("queue", "collaborator"),
    ("dataset", "read"),
    ("dataset", "read_write"),
    ("dataset", "collaborator"),
    ("registry", "read"),
    ("registry", "collaborator"),
    ("credential", "read"),
    ("credential", "collaborator"),
])
def test_the_levels_allowed_per_resource_type_are_accepted(resource_type, level):
    make(resource_type=resource_type, level=level).validate()


def test_a_resource_type_outside_the_four_is_refused():
    with pytest.raises(ValidationError):
        make(resource_type="project").validate()


def test_a_scope_outside_the_three_is_refused():
    with pytest.raises(ValidationError):
        make(scope="company").validate()


def test_a_projects_share_needs_a_project():
    with pytest.raises(ValidationError):
        make(scope="projects", project="").validate()


def test_a_projects_share_must_not_carry_a_user():
    with pytest.raises(ValidationError):
        make(scope="projects", project="p1", user="u2").validate()


def test_a_public_share_must_not_carry_a_user():
    with pytest.raises(ValidationError):
        make(scope="public", project="", user="u2").validate()


def test_a_user_share_is_valid_for_a_queue_at_read_with_no_project():
    share = make(scope="user", project="", user="u2", level="read")
    share.validate()
    assert share.user == "u2"


def test_a_user_share_needs_a_user():
    with pytest.raises(ValidationError):
        make(scope="user", project="", user="", level="read").validate()


def test_a_user_share_must_not_carry_a_project():
    with pytest.raises(ValidationError):
        make(scope="user", project="p1", user="u2", level="read").validate()


@pytest.mark.parametrize("resource_type", ["dataset", "registry", "credential"])
def test_a_user_share_is_refused_for_any_type_but_queue(resource_type):
    with pytest.raises(ValidationError):
        make(scope="user", project="", user="u2", resource_type=resource_type, level="read").validate()


@pytest.mark.parametrize("level", ["read_write", "collaborator"])
def test_a_user_share_is_refused_above_read(level):
    with pytest.raises(ValidationError):
        make(scope="user", project="", user="u2", level=level).validate()


def test_an_unknown_level_is_refused():
    with pytest.raises(ValidationError):
        make(level="admin").validate()
