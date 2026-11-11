"""The repositories service: the only service for code repositories, datasets and registries. What a person's machine and a job
get, assigning by address, and that the old services are gone."""
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.bll import repositories as bll
from apiserver.database.model.auth import User
from apiserver.database.model.project import Project, ProjectMember, ProjectRole
from apiserver.database.model.repository import Assignment, RegistryEntry, Repository
from apiserver.database.model.share import Share
from apiserver.database.model.task.task import Execution, Task, TaskStatus
from apiserver.database.model.user_access import UserAccess
from apiserver.idp import git_credentials as store
from apiserver.service_repo import ServiceRepo
from apiserver.services import repositories as svc
from apiserver.tests.db import ensure_db

ensure_db()
URL = "https://github.com/acme/cars"


@pytest.fixture
def world(monkeypatch):
    company = f"c-svc-{uuid4().hex[:8]}"
    now = datetime.utcnow()
    for user, role in (("admin", "admin"), ("owner", "user"), ("member", "user"), ("stranger", "user")):
        User(id=user, name=user.title(), company=company, role=role, created=now).save()
    root = f"p-{company}"
    Project(id=root, company=company, user="owner", name="P", basename="P", created=now, last_update=now,
            members=[ProjectMember(user="member", role=ProjectRole.read, added=now, added_by="owner")]).save()
    state = SimpleNamespace(company=company, root=root, private=True, audits=[])
    monkeypatch.setattr(store, "key_material", lambda: "test-key-material")
    monkeypatch.setattr(bll, "is_private", lambda address: state.private)
    monkeypatch.setattr(bll, "_check_reads", lambda entry, repo: None)
    monkeypatch.setattr(bll, "_company_reach", lambda repo: (None, "no company GitHub App is set up"))
    monkeypatch.setattr(svc, "audit", lambda actor, action, **detail: state.audits.append((actor, action)))
    monkeypatch.setattr(svc, "client_address", lambda call: "127.0.0.1")
    yield state
    for model in (Repository, RegistryEntry, Assignment, Share, UserAccess, Task, Project, User):
        model.objects(company=company).delete()


def call(user, data, role="user", token_kind="client", job_task=None):
    return SimpleNamespace(data=data, identity=SimpleNamespace(user=user, role=role),
                           auth=SimpleNamespace(token_kind=token_kind, job_task=job_task), result=SimpleNamespace(data=None))


def run(endpoint, world, user, data, **kwargs):
    c = call(user, data, **kwargs)
    endpoint(c, world.company, None)
    return c.result.data


def credential(world, entry_id, user, scope="user"):
    UserAccess(id=entry_id, company=world.company, user=user, kind="app", app_id="1", label=entry_id, scope=scope, host="github.com",
               path="", secret=store.encrypt("pem", store.key_material()), created=datetime.utcnow()).save()


# --- the services ---------------------------------------------------------------------------------------------------------------

def test_the_old_services_are_gone_and_the_new_one_is_there():
    names = ServiceRepo.endpoint_names()
    assert not [n for n in names if n.startswith(("datasets.", "dataset_registry.", "repo_access."))]
    assert {"repositories.get_all", "repositories.add", "repositories.assign", "repositories.unassign", "repositories.check",
            "repositories.set_shared", "repositories.link", "repositories.unlink", "repositories.refresh", "repositories.git_token",
            "repositories.clone", "repositories.find", "repositories.resolve", "repositories.requirement",
            "repositories.get_details", "repositories.delete"} <= set(names)
    assert not {"user_access.grant", "user_access.revoke", "user_access.project_grants"} & set(names)


def test_the_old_modules_and_records_are_gone():
    from importlib.util import find_spec

    for module in ("apiserver.bll.datasets", "apiserver.bll.dataset_registry", "apiserver.bll.dataset_inheritance",
                   "apiserver.bll.dataset_cache", "apiserver.bll.repo_access", "apiserver.database.model.dataset",
                   "apiserver.database.model.code_repository"):
        assert find_spec(module) is None, module


# --- a person's token ------------------------------------------------------------------------------------------------------------

def test_a_public_repository_is_read_without_a_token_by_anyone_who_sees_it(world):
    world.private = False
    bll.add(world.company, "owner", False, URL, "dataset", project_id=world.root)
    answer = run(svc.git_token, world, "member", {"repository": URL, "level": "read"})
    assert answer == {"token": None, "expires_at": None, "username": None}
    assert world.audits[-1] == ("member", "git_token.public")


def test_a_repository_nobody_lists_for_the_user_is_refused_with_the_hint(world):
    bll.add(world.company, "owner", False, URL, "dataset")
    with pytest.raises(errors.bad_request.ValidationError, match="assigning a valid credential to it"):
        run(svc.git_token, world, "stranger", {"repository": URL, "level": "read"})
    with pytest.raises(errors.bad_request.ValidationError, match="no such repository"):
        run(svc.git_token, world, "stranger", {"repository": "https://github.com/acme/unknown", "level": "read"})


def test_a_job_or_an_agent_is_refused_and_the_level_is_checked(world):
    for kind in ("job", "agent"):
        with pytest.raises(errors.forbidden.NoWritePermission):
            run(svc.git_token, world, "owner", {"repository": URL, "level": "read"}, token_kind=kind)
    with pytest.raises(errors.bad_request.ValidationError, match="level must be"):
        run(svc.git_token, world, "owner", {"repository": URL, "level": "admin"})


def test_only_the_owner_writes(world):
    world.private = False
    bll.add(world.company, "owner", False, URL, "dataset", project_id=world.root)
    with pytest.raises(errors.forbidden.NoWritePermission):
        run(svc.git_token, world, "member", {"repository": URL, "level": "read_write"})


# --- assigning, by address ---------------------------------------------------------------------------------------------------------

def test_assigning_by_address_lists_the_repository_for_the_user_and_assigns(world):
    credential(world, "mine", "stranger")
    row = run(svc.assign, world, "stranger", {"repository": URL, "role": "dataset", "credential": "mine"})["repository"]
    assert row["name"] == "cars" and row["roles"] == ["dataset"] and row["owner"] == "stranger"
    assert [a["credential"] for a in row["assignments"]] == ["mine"] and row["assignments"][0]["can_edit"]
    assert Repository.objects(company=world.company).count() == 1


def test_assigning_to_an_address_somebody_else_lists_privately_is_refused(world):
    credential(world, "mine", "stranger")
    bll.add(world.company, "owner", False, URL, "dataset")
    with pytest.raises(errors.bad_request.ValidationError, match="already listed by someone else"):
        run(svc.assign, world, "stranger", {"repository": URL, "credential": "mine"})


def test_unassigning_clears_only_the_callers_own(world):
    credential(world, "a", "owner")
    credential(world, "b", "member")
    repo = bll.add(world.company, "owner", False, URL, "dataset", project_id=world.root)
    bll.assign(world.company, "owner", repo.id, "a")
    bll.assign(world.company, "member", repo.id, "b")
    row = run(svc.unassign, world, "member", {"repository": URL})["repository"]
    assert [a["user"] for a in row["assignments"]] == ["owner"] and not row["assignments"][0]["can_edit"]


def test_the_admin_shares_a_repository_and_a_user_cannot(world):
    repo = bll.add(world.company, "owner", False, URL, "dataset")
    with pytest.raises(errors.forbidden.NoWritePermission):
        run(svc.add, world, "owner", {"url": "https://github.com/acme/other", "role": "dataset", "shared": True})
    row = run(svc.set_shared, world, "admin", {"id": repo.id, "shared": True}, role="admin")["repository"]
    assert row["shared"] and row["owner_name"] == "Company"
    assert [r["name"] for r in run(svc.get_all, world, "stranger", {})["repositories"]] == ["cars"]


# --- finding and cloning ----------------------------------------------------------------------------------------------------------

def test_a_reference_is_found_by_name_by_address_and_by_id(world):
    repo = bll.add(world.company, "owner", False, URL, "dataset")
    for ref in ("cars", URL, URL + ".git", repo.id):
        assert run(svc.find, world, "owner", {"ref": ref})["repository"]["id"] == repo.id
    with pytest.raises(errors.bad_request.ValidationError, match="no such dataset"):
        run(svc.find, world, "stranger", {"ref": "cars"})


def make_job(world, enqueuer="member"):
    task = Task(id=uuid4().hex, company=world.company, user="owner", name="train", type="training", created=datetime.utcnow(),
                started=datetime.utcnow(), status=TaskStatus.in_progress, project=world.root,
                execution=Execution(enqueued_by=enqueuer))
    task.save()
    return task


def test_a_job_finds_and_clones_what_its_project_lists(world):
    world.private = False
    repo = bll.record_use(world.company, URL, "dataset", "owner", world.root)
    job = make_job(world)
    assert run(svc.find, world, "owner", {"ref": "cars"}, token_kind="job", job_task=job.id)["repository"]["id"] == repo.id
    answer = run(svc.clone, world, "owner", {"ref": "cars", "commit": "abc"}, token_kind="job", job_task=job.id)
    assert answer["repository"] == "https://github.com/acme/cars.git" and answer["token"] is None and answer["commit"] == "abc"


def test_a_job_does_not_find_what_neither_its_user_nor_its_project_sees(world):
    bll.add(world.company, "owner", False, URL, "dataset")            # owned by the owner, listed in no project
    job = make_job(world, enqueuer="stranger")
    with pytest.raises(errors.bad_request.ValidationError, match="no such dataset"):
        run(svc.find, world, "stranger", {"ref": "cars"}, token_kind="job", job_task=job.id)


def test_cloning_a_private_repository_nobody_can_read_is_refused_with_the_reason(world):
    bll.add(world.company, "owner", False, URL, "dataset")
    with pytest.raises(errors.bad_request.ValidationError, match="no credential is assigned"):
        run(svc.clone, world, "owner", {"ref": "cars"})


def test_a_registry_is_read_the_moment_it_is_listed(world, monkeypatch):
    from apiserver.bll import repository_registry as registry_bll

    class Reader:
        def read_dir_texts(self, repo, directory):
            return {"cars.yaml": "source: huggingface\nlink: hf://datasets/acme/cars\n"}

        def read_file(self, repo, path, ref=None):
            return None

    monkeypatch.setattr(registry_bll, "reader_for", lambda repo, user, project_root=None: Reader())
    world.private = False
    answer = run(svc.add, world, "owner", {"url": "https://github.com/acme/registry", "role": "registry", "project": world.root})
    assert [e["name"] for e in answer["repository"]["entries"]] == ["cars"]


def test_a_registry_that_cannot_be_read_is_still_listed(world, monkeypatch):
    from apiserver.bll import repository_registry as registry_bll

    def refuse(repo, user, project_root=None):
        raise errors.bad_request.ValidationError("nope")

    monkeypatch.setattr(registry_bll, "reader_for", refuse)
    world.private = False
    answer = run(svc.add, world, "owner", {"url": "https://github.com/acme/registry", "role": "registry"})
    assert answer["repository"]["state"] == "no_access" and answer["repository"]["entries"] == []
