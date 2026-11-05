from apiserver.bll import datasets as bll

OWNER = "u1"
OTHER = "u2"


def dataset(**overrides) -> dict:
    values = {"id": "d1", "name": "cars", "owner": OWNER, "visibility": "user", "credential": None}
    values.update(overrides)
    return values


class TestVisibleTo:
    """ Who may see a stored dataset, from its share records (bll/access), against the database """

    def test_user_visibility_is_the_owner_and_admins_only(self, projects):
        dataset_id = add_private_dataset(OWNER, "user")
        assert bll.can_see(COMPANY, OWNER, False, dataset_id)
        assert bll.can_see(COMPANY, OTHER, True, dataset_id)
        assert not bll.can_see(COMPANY, OTHER, False, dataset_id)

    def test_projects_visibility_is_the_owner_admins_and_members_of_an_allocated_project(self, projects):
        dataset_id = add_private_dataset(OWNER, "projects", projects=["p-team"])
        assert bll.can_see(COMPANY, OTHER, False, dataset_id)
        assert not bll.can_see(COMPANY, "u3", False, dataset_id)
        assert bll.can_see(COMPANY, OWNER, False, dataset_id)

    def test_public_visibility_is_every_user(self, projects):
        dataset_id = add_private_dataset(OWNER, "Public")
        assert bll.can_see(COMPANY, OTHER, False, dataset_id)


class TestCredentialState:
    def test_a_public_dataset_needs_no_credential(self):
        assert bll.credential_state(dataset(credential=None), set()) == ("ok", "")

    def test_a_dataset_whose_credential_is_still_there_is_ok(self):
        assert bll.credential_state(dataset(credential="a1"), {"a1"}) == ("ok", "")

    def test_a_dataset_whose_credential_is_gone_has_no_access_with_the_banner_text(self):
        assert bll.credential_state(dataset(credential="a1"), set()) == (
            "no_access", "no access to dataset cars")


# --- Adding, against the database. GitHub is replaced by fakes.

from types import SimpleNamespace

from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.database.model.dataset import Dataset
from apiserver.tests.db import ensure_db

ensure_db()
COMPANY = f"c-datasets-test-{uuid4().hex[:8]}"


def _remove_rows():
    for model in (Dataset, Project, UserAccess, Share):
        model.objects(company=COMPANY).delete()


@pytest.fixture(autouse=True)
def clean_rows():
    """Every test has a company of its own rows: nothing is left by an earlier test, so the listing shows only this
    company's rows."""
    _remove_rows()
    yield
    _remove_rows()


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


# --- Listing and permissions, against the database.

from datetime import datetime

from apiserver.database.model.project import Project, ProjectMember, ProjectRole
from apiserver.database.model.share import Share
from apiserver.database.model.user_access import UserAccess


def make_project(project_id: str, owner: str, members=()) -> None:
    project = Project(id=project_id, company=COMPANY, user=owner, name=project_id, basename=project_id,
                      created=datetime.utcnow())
    for member in members:
        project.members.append(ProjectMember(user=member, role=ProjectRole.collaborator, added=datetime.utcnow(),
                                             added_by=owner))
    project.save()


@pytest.fixture
def projects():
    Project.objects(company=COMPANY).delete()
    make_project("p-team", "u1", members=["u2"])
    make_project("p-other", "u3")
    yield
    Project.objects(company=COMPANY).delete()


def add_private_dataset(owner: str, visibility: str = "user", projects=None, credential=None) -> str:
    """A dataset with the share records the writers keep (Public, and one project share for each allocated project,
    granted by the owner of the project)."""
    dataset = Dataset(id=f"d-{owner}-{visibility}", company=COMPANY, kind="repo", ref=f"acme/{owner}-{visibility}",
                      name=f"{owner}-{visibility}", owner=owner, visibility=visibility,
                      credential=credential, created=datetime.utcnow(), updated=datetime.utcnow(),
                      last_checked=datetime.utcnow())
    dataset.save()
    if visibility == "Public":
        Share(id=uuid4().hex, company=COMPANY, resource_type="dataset", resource_id=dataset.id, scope="public",
              level="read", granted_by=owner, created=datetime.utcnow()).save()
    for root in projects or []:
        Share(id=uuid4().hex, company=COMPANY, resource_type="dataset", resource_id=dataset.id, scope="projects",
              project=root, level="read", granted_by="u1", created=datetime.utcnow()).save()
    return dataset.id


def test_a_user_only_dataset_is_listed_to_its_owner_and_not_to_others(projects):
    add_private_dataset("u1", "user")
    assert [r["name"] for r in bll.list_visible(COMPANY, "u1", False)] == ["u1-user"]
    assert bll.list_visible(COMPANY, "u2", False) == []


def test_a_projects_dataset_is_listed_to_the_members_of_its_projects(projects):
    add_private_dataset("u1", "projects", projects=["p-team"])
    assert len(bll.list_visible(COMPANY, "u2", False)) == 1
    assert bll.list_visible(COMPANY, "u3", False) == []


def test_a_public_dataset_is_listed_to_everyone(projects):
    add_private_dataset("u1", "Public")
    assert len(bll.list_visible(COMPANY, "u3", False)) == 1


def test_a_dataset_whose_credential_was_removed_shows_no_access(projects):
    add_private_dataset("u1", "Public", credential="gone-access")
    row = bll.list_visible(COMPANY, "u3", False)[0]
    assert row["state"] == "no_access"
    assert row["state_message"] == "no access to dataset u1-Public"


def test_a_project_member_who_does_not_own_a_user_dataset_cannot_share_it(projects):
    dataset_id = add_private_dataset("u1", "user")
    with pytest.raises(errors.bad_request.ValidationError):
        bll.allocate(COMPANY, "u2", dataset_id, "p-team", False)


def test_the_owner_shares_with_a_project_and_its_members_then_see_it(projects):
    dataset_id = add_private_dataset("u1", "user")
    result = bll.allocate(COMPANY, "u1", dataset_id, "p-team", False)
    assert result["visibility"] == "projects"
    assert result["projects"] == ["p-team"]
    assert len(bll.list_visible(COMPANY, "u2", False)) == 1


def test_a_non_member_cannot_allocate_to_a_project(projects):
    dataset_id = add_private_dataset("u1", "projects", projects=["p-team"])
    with pytest.raises(errors.bad_request.ValidationError):
        bll.allocate(COMPANY, "u3", dataset_id, "p-team", False)


def test_only_the_owner_may_change_visibility_or_delete(projects):
    dataset_id = add_private_dataset("u1", "user")
    with pytest.raises(errors.bad_request.ValidationError):
        bll.set_visibility(COMPANY, "u2", dataset_id, "Public", [], False)
    with pytest.raises(errors.bad_request.ValidationError):
        bll.delete(COMPANY, "u2", dataset_id, False)
    bll.delete(COMPANY, "u1", dataset_id, False)
    assert Dataset.objects(id=dataset_id).first() is None


def test_app_discovered_repositories_belong_to_the_owner_of_the_app_credential(projects, monkeypatch):
    UserAccess.objects(company=COMPANY, scope="company", kind="app").delete()
    app = UserAccess(id="app-1", company=COMPANY, user="admin1", kind="app", label="App", scope="company",
                     secret="x", created=datetime.utcnow())
    app.save()
    monkeypatch.setattr(bll.dataset_cache, "cached_list", lambda: {"datasets": [{"repository": "acme/found"}]})
    try:
        rows = bll.list_visible(COMPANY, "u3", False)
        found = [r for r in rows if r["ref"] == "acme/found"]
        assert found and found[0]["owner"] == "admin1" and found[0]["visibility"] == "Public"
    finally:
        app.delete()


def add_access(access_id: str, owner: str) -> None:
    UserAccess.objects(company=COMPANY, id=access_id).delete()
    UserAccess(id=access_id, company=COMPANY, user=owner, kind="token", label=access_id, host="github.com", path="",
               secret="x", scope="user", created=datetime.utcnow()).save()


def test_sharing_a_dataset_shares_its_credential_when_the_owner_of_the_credential_does_it(projects):
    add_access("cred-u1", "u1")
    dataset_id = add_private_dataset("u1", "Public", credential="cred-u1")
    bll.allocate(COMPANY, "u1", dataset_id, "p-team", False)
    assert Share.objects(company=COMPANY, resource_type="credential", resource_id="cred-u1", scope="projects",
                         project="p-team").count() == 1


def test_another_member_cannot_share_a_dataset_whose_credential_is_not_shared_with_the_project(projects):
    add_access("cred-u1", "u1")
    dataset_id = add_private_dataset("u1", "Public", credential="cred-u1")
    with pytest.raises(errors.bad_request.ValidationError):
        bll.allocate(COMPANY, "u2", dataset_id, "p-team", False)


def test_a_public_dataset_shared_with_a_project_is_recorded_on_that_project(projects):
    dataset_id = add_private_dataset("u1", "Public")
    result = bll.allocate(COMPANY, "u1", dataset_id, "p-team", False)
    assert result["projects"] == ["p-team"]
    bll.unallocate(COMPANY, "u1", dataset_id, "p-team", False)
    assert bll.view(Dataset.objects(id=dataset_id).first())["projects"] == []


def test_a_registry_and_a_repository_are_found_by_their_address_but_an_entry_is_not(projects):
    Dataset(id="reg-1", company=COMPANY, kind="registry", ref="acme/registry", name="registry", owner="u1",
            visibility="Public", created=datetime.utcnow()).save()
    Dataset(id="entry-1", company=COMPANY, kind="registry_entry", ref="acme/registry/cars", name="cars", owner="u1",
            visibility="Public", created=datetime.utcnow()).save()
    assert bll.kept_for_repository(COMPANY, "acme/registry").id == "reg-1"
    assert bll.kept_for_repository(COMPANY, "acme/cars") is None
    assert bll.kept_for_repository(COMPANY, "acme/registry/cars") is None
