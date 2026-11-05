from apiserver.bll import datasets as bll

OWNER = "u1"
OTHER = "u2"


def dataset(**overrides) -> dict:
    values = {"id": "d1", "name": "cars", "owner": OWNER, "visibility": "user", "projects": [], "credential": None}
    values.update(overrides)
    return values


class TestVisibleTo:
    def test_user_visibility_is_the_owner_and_admins_only(self):
        assert bll.visible_to(dataset(), OWNER, set(), False)
        assert bll.visible_to(dataset(), OTHER, set(), True)
        assert not bll.visible_to(dataset(), OTHER, set(), False)

    def test_projects_visibility_is_the_owner_admins_and_members_of_an_allocated_project(self):
        shared = dataset(visibility="projects", projects=["p1"])
        assert bll.visible_to(shared, OTHER, {"p1"}, False)
        assert not bll.visible_to(shared, OTHER, {"p2"}, False)
        assert bll.visible_to(shared, OWNER, set(), False)

    def test_public_visibility_is_every_user(self):
        assert bll.visible_to(dataset(visibility="Public"), OTHER, set(), False)


class TestCredentialState:
    def test_a_public_dataset_needs_no_credential(self):
        assert bll.credential_state(dataset(credential=None), set()) == ("ok", "")

    def test_a_dataset_whose_credential_is_still_there_is_ok(self):
        assert bll.credential_state(dataset(credential="a1"), {"a1"}) == ("ok", "")

    def test_a_dataset_whose_credential_is_gone_has_no_access_with_the_banner_text(self):
        assert bll.credential_state(dataset(credential="a1"), set()) == (
            "no_access", "no access to dataset cars")


class TestCanManage:
    def test_the_owner_and_admins_may_manage(self):
        assert bll.can_manage(dataset(), OWNER, False)
        assert bll.can_manage(dataset(), OTHER, True)

    def test_another_user_may_not_manage(self):
        assert not bll.can_manage(dataset(), OTHER, False)


# --- Adding, against the database. GitHub is replaced by fakes.

from types import SimpleNamespace

import pytest

from apiserver.apierrors import errors
from apiserver.database.model.dataset import Dataset
from apiserver.tests.db import ensure_db

ensure_db()
COMPANY = "c-datasets-test"


@pytest.fixture(autouse=True)
def clean_rows():
    Dataset.objects(company=COMPANY).delete()
    yield
    Dataset.objects(company=COMPANY).delete()


def access(entry_id: str) -> SimpleNamespace:
    return SimpleNamespace(id=entry_id, kind="token", api_url=None, app_id=None)


@pytest.fixture
def github(monkeypatch):
    """Fakes for the GitHub reads: which repositories are public, and which access can read which repository."""
    state = SimpleNamespace(public=set(), readable={}, candidates=[], registry_entries=[])
    monkeypatch.setattr(bll, "_public", lambda repo: repo in state.public)
    monkeypatch.setattr(bll, "_candidates", lambda company, user, root, url: [(a, f"secret-{a.id}") for a in state.candidates])
    monkeypatch.setattr(bll, "_reader", lambda entry, secret: SimpleNamespace(name=entry.id))
    monkeypatch.setattr(bll, "_can_read", lambda reader, repo: reader.name in state.readable.get(repo, ()))
    monkeypatch.setattr(bll, "list_entries", lambda app, registry: state.registry_entries)
    return state


def test_a_public_repository_is_added_without_a_credential(github):
    github.public = {"acme/cars"}
    github.candidates = [access("a1")]
    result = bll.add_repo(COMPANY, "u1", "https://github.com/acme/cars")
    assert result["credential"] is None
    assert result["state"] == "ok"
    assert result["owner"] == "u1"


def test_a_private_repository_takes_the_first_candidate_that_can_read_it(github):
    github.candidates = [access("a1"), access("a2")]
    github.readable = {"acme/private": {"a2"}}
    result = bll.add_repo(COMPANY, "u1", "https://github.com/acme/private")
    assert result["credential"] == "a2"
    assert result["state"] == "ok"


def test_a_private_repository_no_candidate_reads_is_still_added_with_no_access(github):
    github.candidates = [access("a1")]
    result = bll.add_repo(COMPANY, "u1", "https://github.com/acme/private")
    assert result["credential"] is None
    assert result["state"] == "no_access"
    assert result["state_message"] == "no access to dataset private"
    assert Dataset.objects(company=COMPANY, ref="acme/private").count() == 1


def test_a_repository_already_added_is_refused(github):
    github.public = {"acme/cars"}
    bll.add_repo(COMPANY, "u1", "https://github.com/acme/cars")
    with pytest.raises(errors.bad_request.ValidationError):
        bll.add_repo(COMPANY, "u2", "https://github.com/acme/cars")


def test_a_public_registry_adds_every_entry_as_a_dataset(github):
    github.public = {"acme/registry"}
    github.registry_entries = [{"name": "cars", "source": "web", "link": "x"},
                               {"name": "coco", "source": "huggingface", "link": "y"}]
    bll.add_registry(COMPANY, "u1", "https://github.com/acme/registry", visibility="Public")
    entries = Dataset.objects(company=COMPANY, kind="registry_entry").order_by("name")
    assert [e.name for e in entries] == ["cars", "coco"]
    assert all(e.visibility == "Public" and e.credential is None for e in entries)


def test_a_private_registry_nobody_can_read_is_saved_with_no_entries(github):
    github.candidates = [access("a1")]
    github.registry_entries = [{"name": "cars", "source": "web", "link": "x"}]
    result = bll.add_registry(COMPANY, "u1", "https://github.com/acme/secret-registry")
    assert result["state"] == "no_access"
    assert Dataset.objects(company=COMPANY, kind="registry_entry").count() == 0
