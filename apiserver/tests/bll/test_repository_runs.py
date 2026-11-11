"""What a run does to the repositories: it records the ones it uses the moment it says so, a registry is read once with every
dataset it lists, and a dispatch asks how they will be read (apiserver/bll/repository_runs, repository_registry)."""
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.bll import access, repositories as bll, repository_registry as registry, repository_runs as runs
from apiserver.database.model.auth import User
from apiserver.database.model.project import Project, ProjectMember, ProjectRole
from apiserver.database.model.repository import Assignment, RegistryEntry, Repository
from apiserver.database.model.share import Share
from apiserver.database.model.task.task import ParamsItem, Task, TaskStatus
from apiserver.database.model.user_access import UserAccess
from apiserver.idp import git_credentials as store
from apiserver.tests.db import ensure_db

ensure_db()
SECTION = "Datasets"
REGISTRY = "https://github.com/acme/registry"

ENTRY_FILES = {
    "cars.yaml": "source: huggingface\nlink: hf://datasets/acme/cars\nadapter: cars\n",
    "faces.yaml": "source: web\nlink: https://example.org/faces.zip\nadapter: faces\n",
    "notes.txt": "not an entry",
    "ours.yaml": "source: repo\nlink: acme/ours\n",
}
METADATA = {"metadata/cars.json": b'{"modalities": {"image": "image"}, "labels": {"class": "class"}, "splits": {"train": 10}, "samples": 10}'}


class FakeReader:
    """What GitHub says about a registry: the entry files, and the metadata of the entries that have one."""

    def __init__(self, files=None, fail=None):
        self.files, self.fail, self.reads = dict(files if files is not None else ENTRY_FILES), fail, 0

    def read_dir_texts(self, repo, directory):
        self.reads += 1
        if self.fail:
            raise self.fail
        return dict(self.files)

    def read_file(self, repo, path, ref=None):
        return METADATA.get(path)


@pytest.fixture
def world(monkeypatch):
    company = f"c-run-{uuid4().hex[:8]}"
    now = datetime.utcnow()
    for user in ("owner", "member", "stranger"):
        User(id=user, name=user.title(), company=company, created=now).save()
    root = f"p-{company}"
    Project(id=root, company=company, user="owner", name="P", basename="P", created=now, last_update=now,
            members=[ProjectMember(user="member", role=ProjectRole.read, added=now, added_by="owner")]).save()
    state = SimpleNamespace(company=company, root=root, private=False, reader=FakeReader(), reached=[])
    monkeypatch.setattr(store, "key_material", lambda: "test-key-material")
    monkeypatch.setattr(bll, "is_private", lambda address: state.private)
    monkeypatch.setattr(bll, "_company_reach", lambda repo: (None, "no company GitHub App is set up"))
    monkeypatch.setattr(registry, "reader_for", lambda repo, user, project_root=None: state.reader)
    yield state
    for model in (Repository, RegistryEntry, Assignment, Share, UserAccess, Task, Project, User):
        model.objects(company=company).delete()


def make_task(world, user, datasets=(), registries=(), script=None, status=TaskStatus.in_progress):
    hyperparams = {SECTION: {}}
    for alias, repo in datasets:
        hyperparams[SECTION][f"{alias}/repo"] = ParamsItem(section=SECTION, name=f"{alias}/repo", value=repo, type="str")
    for alias, repo in registries:
        for key, value in (("external_repo", repo), ("kind", "external"), ("name", alias)):
            hyperparams[SECTION][f"{alias}/{key}"] = ParamsItem(section=SECTION, name=f"{alias}/{key}", value=value, type="str")
    task = Task(id=uuid4().hex, company=world.company, user=user, name="train", type="training", created=datetime.utcnow(),
                started=datetime.utcnow(), status=status, project=world.root, hyperparams=hyperparams,
                script=SimpleNamespace(repository=script, version_num="abc") if False else None)
    task.save()
    return task


def entry_names(world):
    return sorted(e.name for e in RegistryEntry.objects(company=world.company))


# --- the registry a run read an external dataset from -------------------------------------------------------------------------

def test_a_registry_is_kept_with_every_dataset_it_lists_for_the_owner_of_the_project(world):
    task = make_task(world, "member", registries=[("cars", REGISTRY)])
    runs.publish_run(task)
    repo = Repository.objects(company=world.company, key="github.com/acme/registry").first()
    assert repo.owner == "owner" and repo.roles == ["registry"] and repo.state == "ok" and repo.read_at
    assert entry_names(world) == ["cars", "faces"]                      # the one it used, the one it did not, not the others
    assert Repository.objects(company=world.company).count() == 1       # not a dataset of its own
    link = access.find_share(world.company, "repository", repo.id, "projects", world.root)
    assert link is not None and link.inherited and link.run_id == task.id


def test_the_entries_keep_their_metadata_and_a_reference(world):
    runs.publish_run(make_task(world, "owner", registries=[("cars", REGISTRY)]))
    cars = RegistryEntry.objects(company=world.company, name="cars").first()
    assert cars.source == "huggingface" and cars.link == "hf://datasets/acme/cars" and cars.reference == "cars"
    assert '"samples": 10' in cars.metadata
    assert RegistryEntry.objects(company=world.company, name="faces").first().metadata == ""


def test_a_registry_is_read_once_however_many_runs_use_it(world):
    runs.publish_run(make_task(world, "owner", registries=[("cars", REGISTRY)]))
    runs.publish_run(make_task(world, "member", registries=[("faces", "acme/registry")]))
    assert world.reader.reads == 1 and Repository.objects(company=world.company).count() == 1
    assert Share.objects(company=world.company, resource_type="repository", scope="projects").count() == 1


def test_a_registry_that_cannot_be_read_is_kept_without_entries_and_says_why(world):
    world.reader = FakeReader(fail=errors.bad_request.ValidationError("rate limited"))
    runs.publish_run(make_task(world, "owner", registries=[("cars", REGISTRY)]))
    repo = Repository.objects(company=world.company).first()
    assert repo.state == "no_access" and "could not be read" in repo.state_message and repo.read_at is None
    assert entry_names(world) == [] and access.find_share(world.company, "repository", repo.id, "projects", world.root)


def test_a_registry_that_failed_is_read_again_by_the_next_run(world):
    world.reader = FakeReader(fail=errors.bad_request.ValidationError("rate limited"))
    runs.publish_run(make_task(world, "owner", registries=[("cars", REGISTRY)]))
    world.reader = FakeReader()
    runs.publish_run(make_task(world, "owner", registries=[("cars", REGISTRY)]))
    assert entry_names(world) == ["cars", "faces"] and Repository.objects(company=world.company).first().state == "ok"


def test_refreshing_a_registry_adds_updates_and_removes_entries(world):
    runs.publish_run(make_task(world, "owner", registries=[("cars", REGISTRY)]))
    repo = Repository.objects(company=world.company).first()
    world.reader = FakeReader({"cars.yaml": "source: huggingface\nlink: hf://datasets/acme/cars2\n",
                               "newer.yaml": "source: web\nlink: https://example.org/n.zip\n"})
    counts = registry.refresh(world.company, "owner", False, repo.id)
    assert counts == {"added": 1, "updated": 1, "removed": 1} and entry_names(world) == ["cars", "newer"]


def test_only_the_owner_and_the_admin_refresh_a_registry(world):
    runs.publish_run(make_task(world, "owner", registries=[("cars", REGISTRY)]))
    repo = Repository.objects(company=world.company).first()
    with pytest.raises(errors.forbidden.NoWritePermission):
        registry.refresh(world.company, "member", False, repo.id)
    with pytest.raises(errors.bad_request.ValidationError, match="no such repository"):
        registry.refresh(world.company, "stranger", False, repo.id)


def test_a_repository_that_is_not_a_registry_is_not_refreshed(world):
    repo = bll.add(world.company, "owner", False, "https://github.com/acme/cars", "dataset")
    with pytest.raises(errors.bad_request.ValidationError, match="not a registry"):
        registry.refresh(world.company, "owner", False, repo.id)


def test_a_registry_is_read_with_the_credential_that_reads_it(world, monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(store, "key_material", lambda: "test-key-material")
    monkeypatch.setattr(bll, "is_private", lambda address: True)
    monkeypatch.setattr(bll, "_company_reach", lambda repo: (None, "none"))
    repo = bll.record_use(world.company, REGISTRY, "registry", "owner", world.root)
    with pytest.raises(errors.bad_request.ValidationError, match="could not be read"):
        registry.reader_for(repo, "owner", world.root)           # private, and nobody assigned a credential


# --- datasets of their own ----------------------------------------------------------------------------------------------------

def test_a_dataset_of_its_own_is_recorded_as_a_dataset_the_moment_the_run_records_it(world):
    runs.publish_run(make_task(world, "member", datasets=[("cars", "acme/cars")]))
    repo = Repository.objects(company=world.company, key="github.com/acme/cars").first()
    assert repo.owner == "owner" and repo.roles == ["dataset"] and repo.reference == "cars"
    assert Repository.objects(company=world.company, roles="registry").count() == 0


def test_a_deactivated_user_records_nothing(world):
    User(id="gone", name="Gone", company=world.company, created=datetime.utcnow(), idp_disabled=True).save()
    runs.publish_run(make_task(world, "gone", datasets=[("cars", "acme/cars")]))
    assert Repository.objects(company=world.company).count() == 0


# --- the warning before a dispatch --------------------------------------------------------------------------------------------

def test_a_dispatch_asks_about_a_private_repository_nobody_assigned_and_offers_the_users_credentials(world):
    world.private = True
    UserAccess(id="mine", company=world.company, user="member", kind="app", app_id="1", label="mine", scope="user",
               host="github.com", path="", secret=store.encrypt("pem", store.key_material()), created=datetime.utcnow()).save()
    task = make_task(world, "member", datasets=[("cars", "acme/cars")])
    runs.publish_run(task)
    rows = runs.requirement(world.company, "member", [task.id], False)
    assert len(rows) == 1 and rows[0]["ask"] and rows[0]["state"] == bll.UNREACHABLE and rows[0]["can_assign"]
    assert [c["id"] for c in rows[0]["assignable"]] == ["mine"] and rows[0]["kinds"] == ["dataset"]
    assert rows[0]["tasks"] == [task.id] and rows[0]["project_name"] == "P"


def test_a_public_repository_is_marked_as_needing_a_decision_and_an_assigned_one_is_not(world, monkeypatch):
    world.private = False
    task = make_task(world, "member", datasets=[("cars", "acme/cars")])
    runs.publish_run(task)
    assert runs.requirement(world.company, "member", [task.id], False)[0]["state"] == bll.ANONYMOUS
    repo = Repository.objects(company=world.company).first()
    bll.assign(world.company, "owner", repo.id, none_needed=True)
    row = runs.requirement(world.company, "member", [task.id], False)[0]
    assert row["state"] == bll.NONE_NEEDED and not row["ask"]


def test_a_dispatch_is_not_warned_about_tasks_the_user_may_not_see(world):
    task = make_task(world, "owner", datasets=[("cars", "acme/cars")])
    runs.publish_run(task)
    assert runs.requirement(world.company, "stranger", [task.id], False) == []


def test_the_badges_answer_for_the_repositories_that_are_recorded(world):
    runs.publish_run(make_task(world, "owner", datasets=[("cars", "acme/cars")]))
    rows = runs.resolve_many(world.company, ["https://github.com/acme/cars", "https://github.com/acme/unknown", "nonsense"],
                             "member", world.root)
    assert [r["url"] for r in rows] == ["https://github.com/acme/cars"] and rows[0]["state"] == bll.ANONYMOUS
