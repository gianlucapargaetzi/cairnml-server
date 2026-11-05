# apiserver/tests/bll/test_dataset_readers.py
"""Who sees, gets and allocates a dataset or a registry: the readers of bll/datasets and the dataset services. The first
tests pin the behaviour the readers had before they read the share records (Task 10c). The writers keep the old fields
and the share records in step, so the same answers hold after the switch."""
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.bll import datasets
from apiserver.database.model.auth import Role, User
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.project import Project, ProjectMember, ProjectRole
from apiserver.database.model.user_access import UserAccess
from apiserver.services import dataset_registry as registry_svc
from apiserver.services import datasets as datasets_svc
from apiserver.tests.db import ensure_db

ensure_db()

REPO_URL = "https://github.com/owner/cars"
REGISTRY_URL = "https://github.com/owner/registry"


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


@pytest.fixture
def github(monkeypatch):
    """GitHub is replaced: every repository is public, and a registry lists the entries the test sets."""
    state = SimpleNamespace(entries=[])
    monkeypatch.setattr(datasets, "_public", lambda repo: True)
    monkeypatch.setattr(datasets, "list_entries", lambda app, registry: state.entries)
    return state


@pytest.fixture
def setup(company):
    for user in ("owner1", "u2", "stranger", "admin1"):
        User(id=user, name=user, company=company, role=Role.admin if user == "admin1" else Role.user,
             created=datetime.utcnow()).save()
    make_root(company, "root1", "owner1", members=[("u2", ProjectRole.read)])
    make_root(company, "root2", "owner1")
    make_root(company, "root3", "u2")
    yield
    for model in (Dataset, Project, UserAccess, User):
        model.objects(company=company).delete()


def make_root(company, id_, owner, members=()):
    Project(
        id=id_, company=company, user=owner, name=id_, basename=id_, created=datetime.utcnow(),
        members=[ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by=owner) for u, r in members],
    ).save()


def sees(company, user, dataset_id, is_admin=False):
    return any(row["id"] == dataset_id for row in datasets.list_visible(company, user, is_admin))


def call_as(user, role=Role.user):
    return SimpleNamespace(identity=SimpleNamespace(user=user, role=role),
                           result=SimpleNamespace(data=None, data_model=None), data=None)


# --- datasets.visible: the datasets a user is shown


def test_a_user_dataset_is_seen_by_its_owner_and_by_an_admin_only(company, setup, github):
    dataset = datasets.add_repo(company, "owner1", REPO_URL, visibility="user")
    assert sees(company, "owner1", dataset["id"])
    assert sees(company, "admin1", dataset["id"], is_admin=True)
    assert not sees(company, "stranger", dataset["id"])
    assert not sees(company, "u2", dataset["id"])


def test_a_public_dataset_is_seen_by_every_user(company, setup, github):
    dataset = datasets.add_repo(company, "owner1", REPO_URL, visibility="Public")
    assert sees(company, "stranger", dataset["id"])
    assert sees(company, "u2", dataset["id"])


def test_a_projects_dataset_is_seen_by_the_members_of_its_allocated_project_only(company, setup, github):
    dataset = datasets.add_repo(company, "owner1", REPO_URL, visibility="user")
    datasets.set_visibility(company, "owner1", dataset["id"], "projects", ["root1"], False)
    assert sees(company, "u2", dataset["id"])
    assert not sees(company, "stranger", dataset["id"])
    assert sees(company, "owner1", dataset["id"])


def test_a_member_of_a_project_the_dataset_is_not_allocated_to_does_not_see_it(company, setup, github):
    dataset = datasets.add_repo(company, "owner1", REPO_URL, visibility="user")
    datasets.set_visibility(company, "owner1", dataset["id"], "projects", ["root2"], False)
    assert not sees(company, "u2", dataset["id"])


def test_unallocating_a_dataset_hides_it_from_the_project_members(company, setup, github):
    dataset = datasets.add_repo(company, "owner1", REPO_URL, visibility="user")
    datasets.allocate(company, "owner1", dataset["id"], "root1", False)
    assert sees(company, "u2", dataset["id"])
    datasets.unallocate(company, "owner1", dataset["id"], "root1", False)
    assert not sees(company, "u2", dataset["id"])


def test_a_dataset_whose_visibility_goes_back_to_user_is_hidden_from_the_project(company, setup, github):
    dataset = datasets.add_repo(company, "owner1", REPO_URL, visibility="user")
    datasets.set_visibility(company, "owner1", dataset["id"], "projects", ["root1"], False)
    datasets.set_visibility(company, "owner1", dataset["id"], "user", [], False)
    assert not sees(company, "u2", dataset["id"])


def test_a_dataset_whose_credential_is_gone_shows_no_access_to_its_viewers(company, setup, github):
    dataset = datasets.add_repo(company, "owner1", REPO_URL, visibility="Public")
    Dataset.objects(company=company, id=dataset["id"]).update(credential="gone-access")
    row = next(r for r in datasets.list_visible(company, "stranger", False) if r["id"] == dataset["id"])
    assert row["state"] == "no_access"
    assert row["state_message"] == "no access to dataset cars"


# --- datasets.allocate and unallocate: who may allocate, and what they refuse


def test_the_owner_of_a_user_dataset_may_allocate_it_to_a_project(company, setup, github):
    dataset = datasets.add_repo(company, "owner1", REPO_URL, visibility="user")
    result = datasets.allocate(company, "owner1", dataset["id"], "root1", False)
    assert result["visibility"] == "projects"
    assert result["projects"] == ["root1"]


def test_a_project_member_who_does_not_see_a_user_dataset_may_not_allocate_it(company, setup, github):
    dataset = datasets.add_repo(company, "owner1", REPO_URL, visibility="user")
    with pytest.raises(errors.bad_request.ValidationError, match="no such dataset of yours"):
        datasets.allocate(company, "u2", dataset["id"], "root1", False)


def test_a_user_who_is_not_a_member_of_the_project_may_not_allocate(company, setup, github):
    dataset = datasets.add_repo(company, "owner1", REPO_URL, visibility="user")
    with pytest.raises(errors.bad_request.ValidationError, match="not a member of this project"):
        datasets.allocate(company, "owner1", dataset["id"], "root3", False)


def test_a_member_may_allocate_a_public_dataset_they_see_to_their_project(company, setup, github):
    dataset = datasets.add_repo(company, "owner1", REPO_URL, visibility="Public")
    result = datasets.allocate(company, "u2", dataset["id"], "root1", False)
    assert result["projects"] == ["root1"]


def test_a_member_may_not_change_the_visibility_of_a_dataset_they_do_not_own(company, setup, github):
    dataset = datasets.add_repo(company, "owner1", REPO_URL, visibility="Public")
    with pytest.raises(errors.bad_request.ValidationError, match="only the owner"):
        datasets.set_visibility(company, "u2", dataset["id"], "user", [], False)


def test_an_allocation_of_a_credential_the_project_cannot_use_is_refused_for_a_member_who_does_not_own_it(
        company, setup, github):
    UserAccess(id="cred-owner1", company=company, user="owner1", kind="token", label="cred", host="github.com",
               path="", secret="x", scope="user", created=datetime.utcnow()).save()
    dataset = datasets.add_repo(company, "owner1", REPO_URL, visibility="Public")
    Dataset.objects(company=company, id=dataset["id"]).update(credential="cred-owner1")
    with pytest.raises(errors.bad_request.ValidationError, match="its owner must share it first"):
        datasets.allocate(company, "u2", dataset["id"], "root1", False)


def test_the_owner_of_a_credential_may_allocate_a_dataset_that_uses_it(company, setup, github):
    UserAccess(id="cred-owner1", company=company, user="owner1", kind="token", label="cred", host="github.com",
               path="", secret="x", scope="user", created=datetime.utcnow()).save()
    dataset = datasets.add_repo(company, "owner1", REPO_URL, visibility="Public")
    Dataset.objects(company=company, id=dataset["id"]).update(credential="cred-owner1")
    datasets.allocate(company, "owner1", dataset["id"], "root1", False)
    assert "root1" in UserAccess.objects(company=company, id="cred-owner1").first().granted_projects


def test_a_dataset_that_is_not_visible_cannot_be_allocated(company, setup, github):
    dataset = datasets.add_repo(company, "owner1", REPO_URL, visibility="user")
    with pytest.raises(errors.bad_request.ValidationError, match="no such dataset of yours"):
        datasets.allocate(company, "u2", dataset["id"], "root1", False)


# --- registries and their entries: dataset_registry.get_all and get_registries


def test_a_public_registry_and_its_entries_are_seen_by_every_user(company, setup, github):
    github.entries = [{"name": "cars", "source": "web", "link": "x"}]
    datasets.add_registry(company, "owner1", REGISTRY_URL, visibility="Public")
    assert [e["name"] for e in _run_get_all(company, "stranger").result.data["datasets"]] == ["cars"]


def _run_get_all(company, user, is_admin=False):
    call = call_as(user, Role.admin if is_admin else Role.user)
    registry_svc.get_all(call, company, None)
    return call


def test_a_private_registry_entry_is_seen_by_its_owner_and_not_by_others(company, setup, github):
    github.entries = [{"name": "cars", "source": "web", "link": "x"}]
    datasets.add_registry(company, "owner1", REGISTRY_URL, visibility="user")
    assert [e["name"] for e in _run_get_all(company, "owner1").result.data["datasets"]] == ["cars"]
    assert _run_get_all(company, "stranger").result.data["datasets"] == []


def test_an_entry_names_the_registry_that_lists_it(company, setup, github):
    github.entries = [{"name": "cars", "source": "web", "link": "x"}]
    datasets.add_registry(company, "owner1", REGISTRY_URL, visibility="Public")
    entry = _run_get_all(company, "stranger").result.data["datasets"][0]
    assert entry["registry"] == "owner/registry"


def test_a_registry_is_listed_by_get_registries_to_the_users_who_see_it(company, setup, github):
    datasets.add_registry(company, "owner1", REGISTRY_URL, visibility="user")
    call = call_as("owner1")
    registry_svc.get_registries(call, company, None)
    assert [r["repository"] for r in call.result.data["registries"]] == ["owner/registry"]
    other = call_as("stranger")
    registry_svc.get_registries(other, company, None)
    assert other.result.data["registries"] == []


def test_a_project_member_sees_a_registry_allocated_to_their_project(company, setup, github):
    registry = datasets.add_registry(company, "owner1", REGISTRY_URL, visibility="user")
    datasets.allocate(company, "owner1", registry["id"], "root1", False)
    call = call_as("u2")
    registry_svc.get_registries(call, company, None)
    assert [r["repository"] for r in call.result.data["registries"]] == ["owner/registry"]


# --- datasets.get_details: a dataset the caller may not see is refused as missing


def details_call(user, data, role=Role.user):
    call = call_as(user, role)
    call.data = data
    return call


def test_get_details_refuses_a_repository_the_caller_may_not_see_as_no_such_dataset(company, setup, github):
    datasets.add_repo(company, "owner1", REPO_URL, visibility="user")
    with pytest.raises(errors.bad_request.ValidationError, match="no such dataset"):
        datasets_svc.get_details(details_call("stranger", {"repository": "owner/cars"}), company, None)


def test_get_details_refuses_a_registry_entry_the_caller_may_not_see_as_no_such_dataset(company, setup, github):
    github.entries = [{"name": "cars", "source": "web", "link": "x"}]
    datasets.add_registry(company, "owner1", REGISTRY_URL, visibility="user")
    with pytest.raises(errors.bad_request.ValidationError, match="no such dataset"):
        datasets_svc.get_details(details_call("stranger", {"registry_name": "cars"}), company, None)


def test_get_details_for_a_repository_that_is_not_kept_asks_no_visibility(company, setup, github, monkeypatch):
    """No record of the repository: nothing to check, so the call goes on to GitHub (here replaced)."""
    seen = {}

    def details(app, **kwargs):
        seen.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(datasets_svc.bll, "details", details)
    monkeypatch.setattr(datasets_svc.github_check, "reader", lambda: None)
    datasets_svc.get_details(details_call("stranger", {"repository": "owner/unkept"}), company, None)
    assert seen["repository"] == "owner/unkept"


# --- the readers read the share records: a share alone gives access, the old field alone gives none


def add_bare_dataset(company, dataset_id, owner, visibility="user", projects=None):
    """A dataset row with the old fields set and no share record of its own."""
    Dataset(id=dataset_id, company=company, kind="repo", ref=f"owner/{dataset_id}", name=dataset_id, owner=owner,
            visibility=visibility, projects=projects or [], created=datetime.utcnow(),
            updated=datetime.utcnow()).save()
    return dataset_id


def add_share(company, dataset_id, scope, granted_by="owner1", project="", user=""):
    from apiserver.database.model.share import Share
    Share(id=uuid4().hex, company=company, resource_type="dataset", resource_id=dataset_id, scope=scope,
          project=project, user=user, level="read", granted_by=granted_by, created=datetime.utcnow()).save()


def test_a_public_share_alone_gives_every_user_access_with_an_empty_old_field(company, setup, github):
    add_bare_dataset(company, "share-public", "owner1", visibility="user")
    add_share(company, "share-public", "public")
    assert sees(company, "stranger", "share-public")


def test_a_project_share_alone_gives_the_members_of_the_project_access_with_an_empty_old_field(company, setup, github):
    add_bare_dataset(company, "share-project", "owner1", visibility="user", projects=[])
    add_share(company, "share-project", "projects", project="root1")
    assert sees(company, "u2", "share-project")
    assert not sees(company, "stranger", "share-project")


def test_the_old_field_alone_gives_no_access(company, setup, github):
    """A Public visibility or an allocation in the old fields, with no share record: the readers give nothing."""
    add_bare_dataset(company, "old-public", "owner1", visibility="Public")
    add_bare_dataset(company, "old-project", "owner1", visibility="projects", projects=["root1"])
    assert not sees(company, "stranger", "old-public")
    assert not sees(company, "u2", "old-project")


def test_a_deactivated_owner_sees_none_of_their_datasets(company, setup, github):
    dataset = datasets.add_repo(company, "owner1", REPO_URL, visibility="user")
    User.objects(id="owner1", company=company).update(idp_disabled=True)
    assert not sees(company, "owner1", dataset["id"])


def test_a_project_share_granted_by_a_deactivated_owner_gives_no_access(company, setup, github):
    dataset = datasets.add_repo(company, "owner1", REPO_URL, visibility="user")
    datasets.allocate(company, "owner1", dataset["id"], "root1", False)
    assert sees(company, "u2", dataset["id"])
    User.objects(id="owner1", company=company).update(idp_disabled=True)
    assert not sees(company, "u2", dataset["id"])


# --- dataset_credentials: who gets which level of a dataset's keys (Task 10c, before the switch)


from apiserver.bll import dataset_credentials as keys


class TestDatasetKeyLevels:
    def test_the_owner_gets_read_and_read_write(self, company, setup, github):
        datasets.add_repo(company, "owner1", REPO_URL, visibility="user")
        assert keys._authorize(company, "owner1", "owner/cars", "read", False).endswith("owner/cars")
        assert keys._authorize(company, "owner1", "owner/cars", "read_write", False).endswith("owner/cars")

    def test_a_stranger_gets_no_key_to_a_user_dataset(self, company, setup, github):
        datasets.add_repo(company, "owner1", REPO_URL, visibility="user")
        with pytest.raises(errors.bad_request.ValidationError, match="no such dataset"):
            keys._authorize(company, "stranger", "owner/cars", "read", False)

    def test_a_project_member_reads_a_dataset_allocated_to_the_project_but_may_not_write_it(self, company, setup,
                                                                                            github):
        dataset = datasets.add_repo(company, "owner1", REPO_URL, visibility="user")
        datasets.allocate(company, "owner1", dataset["id"], "root1", False)
        assert keys._authorize(company, "u2", "owner/cars", "read", False).endswith("owner/cars")
        with pytest.raises(errors.forbidden.NoWritePermission):
            keys._authorize(company, "u2", "owner/cars", "read_write", False)

    def test_a_public_dataset_is_readable_by_everyone_and_writable_only_by_its_owner(self, company, setup, github):
        datasets.add_repo(company, "owner1", REPO_URL, visibility="Public")
        assert keys._authorize(company, "stranger", "owner/cars", "read", False).endswith("owner/cars")
        with pytest.raises(errors.forbidden.NoWritePermission):
            keys._authorize(company, "stranger", "owner/cars", "read_write", False)

    def test_an_admin_gets_read_write_on_someone_elses_dataset(self, company, setup, github):
        datasets.add_repo(company, "owner1", REPO_URL, visibility="user")
        assert keys._authorize(company, "admin1", "owner/cars", "read_write", True).endswith("owner/cars")

    def test_a_new_name_is_writable_under_the_company_prefix_and_not_readable(self, company, setup, github):
        assert keys._authorize(company, "stranger", "owner/new-one", "read_write", False).endswith("owner/new-one")
        with pytest.raises(errors.bad_request.ValidationError):
            keys._authorize(company, "stranger", "owner/new-one", "read", False)

    def test_a_dataset_whose_visibility_goes_back_to_user_is_no_longer_readable_by_the_project(self, company, setup,
                                                                                               github):
        datasets.add_repo(company, "owner1", REPO_URL, visibility="user")
        datasets.set_visibility(company, "owner1", datasets.Dataset.objects(company=company).first().id,
                                "projects", ["root1"], False)
        assert keys._authorize(company, "u2", "owner/cars", "read", False)
        datasets.set_visibility(company, "owner1", datasets.Dataset.objects(company=company).first().id,
                                "user", [], False)
        with pytest.raises(errors.bad_request.ValidationError, match="no such dataset"):
            keys._authorize(company, "u2", "owner/cars", "read", False)
