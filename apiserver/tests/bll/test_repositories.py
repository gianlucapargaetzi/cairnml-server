"""A repository a run uses is recorded once per company, and once per root project, owned by the project's owner. One
resolver says how it is read; the assignment rights follow the owner / first user / admin rule."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.bll import repositories as bll
from apiserver.database.model.code_repository import CodeRepository, RepositoryBinding
from apiserver.database.model.project import Project
from apiserver.database.model.share import Share
from apiserver.database.model.user_access import UserAccess
from apiserver.idp import git_credentials as store
from apiserver.idp.github import RepoNotAccessible
from apiserver.tests.db import ensure_db

ensure_db()
URL = "https://github.com/acme/proj"


@pytest.fixture
def world(monkeypatch):
    company = f"c-repo-{uuid4().hex[:8]}"
    probe = SimpleNamespace(private=False, calls=0)

    def fake_private(_repository):
        probe.calls += 1
        return probe.private

    monkeypatch.setattr(store, "key_material", lambda: "test-key-material")
    monkeypatch.setattr(bll, "is_private", fake_private)
    monkeypatch.setattr(bll, "_github_app", lambda: None)
    monkeypatch.setattr(bll, "_check_reads", lambda entry, source: None)
    monkeypatch.setattr(bll.user_access, "is_active", lambda user: user != "gone")
    Project(id=f"p-{company}", company=company, name="p", basename="p", user="owner", created=datetime.utcnow(),
            last_update=datetime.utcnow()).save()
    yield SimpleNamespace(company=company, project=f"p-{company}", probe=probe, monkeypatch=monkeypatch)
    for model in (CodeRepository, RepositoryBinding, UserAccess, Share, Project):
        model.objects(company=company).delete()


def credential(world, entry_id, user, kind="app", scope="user", host="github.com", path=""):
    UserAccess(id=entry_id, company=world.company, user=user, kind=kind, app_id="1", label=f"cred {entry_id}",
               scope=scope, host=host, path=path, secret=store.encrypt("pem", store.key_material()),
               created=datetime.utcnow()).save()
    return entry_id


def discover(world, url=URL, user="owner"):
    return bll.discover(world.company, url, user, world.project)


def test_every_spelling_of_an_address_is_one_source_and_one_binding(world):
    spellings = [URL, URL + ".git", "git@github.com:Acme/Proj.git", "ssh://git@github.com/acme/proj", "GIT://GitHub.com/ACME/proj/"]
    ids = {discover(world, spelling).id for spelling in spellings}
    assert len(ids) == 1
    assert CodeRepository.objects(company=world.company).count() == 1
    assert RepositoryBinding.objects(company=world.company).count() == 1


def test_a_binding_is_owned_by_the_projects_owner_and_found_by_the_first_user(world):
    binding = discover(world, user="alice")
    assert binding.owner == "owner" and binding.discovered_by == "alice" and binding.project == world.project
    again = discover(world, user="bob")
    assert again.id == binding.id and again.discovered_by == "alice"


def test_a_second_project_gets_its_own_binding_of_the_same_source(world):
    Project(id=f"q-{world.company}", company=world.company, name="q", basename="q", user="other", created=datetime.utcnow(),
            last_update=datetime.utcnow()).save()
    first = discover(world)
    second = bll.discover(world.company, URL, "other", f"q-{world.company}")
    assert first.source == second.source and first.id != second.id and second.owner == "other"


def test_a_run_outside_a_project_binds_to_its_user(world):
    binding = bll.discover(world.company, URL, "dana", None)
    assert binding.scope == "user:dana" and binding.owner == "dana" and binding.project is None


def test_an_address_that_names_no_repository_makes_no_record(world):
    assert bll.discover(world.company, "/home/me/code", "owner", world.project) is None
    assert bll.discover(world.company, "", "owner", world.project) is None


def test_the_anonymous_answer_is_kept_and_asked_again_only_when_stale(world):
    binding = discover(world)
    source = CodeRepository.objects(id=binding.source).first()
    assert bll.anonymous(source) is True and world.probe.calls == 1
    assert bll.anonymous(source) is True and world.probe.calls == 1
    later = datetime.utcnow() + bll.ANONYMOUS_YES_TTL + timedelta(seconds=1)
    assert bll.anonymous(source, later) is True and world.probe.calls == 2


def test_an_unknown_answer_is_asked_again_after_a_minute_and_never_refuses(world):
    world.probe.private = None
    binding = discover(world)
    source = CodeRepository.objects(id=binding.source).first()
    assert bll.anonymous(source) is None
    assert bll.resolve(source, binding).state == bll.UNKNOWN
    assert bll.token_for_job(source, binding)["token"] is None
    assert bll.anonymous(source, datetime.utcnow() + bll.ANONYMOUS_UNKNOWN_TTL + timedelta(seconds=1)) is None
    assert world.probe.calls == 2


def test_unassigned_public_private_and_company_reached_repositories(world):
    binding = discover(world)
    source = CodeRepository.objects(id=binding.source).first()
    assert bll.resolve(source, binding).state == bll.ANONYMOUS
    world.probe.private = True
    source.anonymous_checked = None
    assert bll.resolve(source, binding).state == bll.UNREACHABLE
    with pytest.raises(errors.bad_request.ValidationError, match="private"):
        bll.token_for_job(source, binding)
    world.monkeypatch.setattr(bll, "_company_reach", lambda src: ("Company Github App", None))
    resolution = bll.resolve(source, binding)
    assert resolution.state == bll.COMPANY and "Auto-Authenticated via Company Github App" in resolution.message


def test_an_assigned_credential_is_the_only_one_used(world):
    binding = discover(world)
    source = CodeRepository.objects(id=binding.source).first()
    world.probe.private = False  # public, yet the assignment decides
    credential(world, "app-1", "owner")
    bll.set_credential(world.company, "owner", False, binding.id, "app-1")
    binding.reload()
    resolution = bll.resolve(source, binding)
    assert resolution.state == bll.ASSIGNED and resolution.entry.id == "app-1"
    UserAccess.objects(id="app-1").delete()
    gone = bll.resolve(source, binding)
    assert gone.state == bll.UNREACHABLE and "no longer exists" in gone.message
    world.monkeypatch.setattr(bll, "_company_reach", lambda src: ("Company Github App", None))
    assert bll.resolve(source, binding).state == bll.UNREACHABLE  # no fallback


def test_a_credential_of_a_disabled_owner_is_not_usable(world):
    binding = discover(world)
    source = CodeRepository.objects(id=binding.source).first()
    credential(world, "app-gone", "gone")
    binding.credential = "app-gone"
    binding.save()
    assert bll.resolve(source, binding).state == bll.UNREACHABLE


def test_none_needed_needs_a_repository_the_host_clones_anonymously(world):
    binding = discover(world)
    bll.set_credential(world.company, "owner", False, binding.id, None, none_needed=True)
    binding.reload()
    assert binding.none_needed is True
    world.probe.private = True
    with pytest.raises(errors.bad_request.ValidationError, match="cannot be cloned without a credential"):
        bll.set_credential(world.company, "owner", False, binding.id, None, none_needed=True)


def test_owner_chooses_any_own_credential_or_the_companys(world):
    binding = discover(world)
    credential(world, "mine", "owner")
    credential(world, "theirs", "alice")
    credential(world, "corp", "admin", scope="company")
    bll.set_credential(world.company, "owner", False, binding.id, "mine")
    bll.set_credential(world.company, "owner", False, binding.id, "corp")
    with pytest.raises(errors.bad_request.ValidationError, match="not one you may assign"):
        bll.set_credential(world.company, "owner", False, binding.id, "theirs")


def test_the_first_user_chooses_only_owner_credentials_shared_with_the_project(world):
    binding = discover(world, user="alice")
    credential(world, "owned-unshared", "owner")
    credential(world, "owned-shared", "owner")
    credential(world, "alices", "alice")
    Share(id=uuid4().hex, company=world.company, resource_type="credential", resource_id="owned-shared",
          scope="projects", project=world.project, level="read", granted_by="owner",
          created=datetime.utcnow()).save()
    source = CodeRepository.objects(id=binding.source).first()
    rows = bll.assignable(world.company, binding, source, "alice", False)
    assert [row["id"] for row in rows] == ["owned-shared"]
    for refused in ("owned-unshared", "alices"):
        with pytest.raises(errors.bad_request.ValidationError):
            bll.set_credential(world.company, "alice", False, binding.id, refused)
    bll.set_credential(world.company, "alice", False, binding.id, "owned-shared")


def test_an_admin_chooses_only_company_credentials_and_none_needed(world):
    binding = discover(world)
    credential(world, "mine", "admin")
    credential(world, "corp", "boss", scope="company")
    with pytest.raises(errors.bad_request.ValidationError):
        bll.set_credential(world.company, "admin", True, binding.id, "mine")
    bll.set_credential(world.company, "admin", True, binding.id, "corp")
    bll.set_credential(world.company, "admin", True, binding.id, None, none_needed=True)


def test_anyone_else_assigns_nothing(world):
    binding = discover(world)
    with pytest.raises(errors.forbidden.NoWritePermission):
        bll.set_credential(world.company, "stranger", False, binding.id, None, none_needed=True)


def test_a_github_token_credential_is_refused(world):
    binding = discover(world)
    credential(world, "tok", "owner", kind="token")
    with pytest.raises(errors.bad_request.ValidationError, match="personal token"):
        bll.set_credential(world.company, "owner", False, binding.id, "tok")


def test_another_host_takes_a_token_valid_for_its_host_and_path(world):
    binding = bll.discover(world.company, "https://git.example.org/team/app.git", "owner", world.project)
    credential(world, "good", "owner", kind="token", host="git.example.org", path="team")
    credential(world, "wrong", "owner", kind="token", host="git.example.org", path="other")
    bll.set_credential(world.company, "owner", False, binding.id, "good")
    with pytest.raises(errors.bad_request.ValidationError, match="not valid for"):
        bll.set_credential(world.company, "owner", False, binding.id, "wrong")


def test_the_list_has_one_row_per_repository_and_a_binding_per_project(world):
    Project(id=f"q-{world.company}", company=world.company, name="q", basename="q", user="other", created=datetime.utcnow(),
            last_update=datetime.utcnow()).save()
    discover(world)
    bll.discover(world.company, URL + ".git", "other", f"q-{world.company}")
    rows = bll.list_for(world.company, "admin", True)
    assert len(rows) == 1 and len(rows[0]["bindings"]) == 2
    mine = bll.list_for(world.company, "owner", False)
    assert len(mine) == 1 and [b["owner"] for b in mine[0]["bindings"]] == ["owner"]
    assert mine[0]["bindings"][0]["can_assign"] is True
    assert bll.list_for(world.company, "stranger", False) == []


def test_adding_lists_a_repository_for_a_member_and_refuses_a_stranger(world):
    binding = bll.add(world.company, "owner", False, "git@github.com:Acme/Proj.git", world.project)
    assert binding.project == world.project
    with pytest.raises(errors.forbidden.NoWritePermission):
        bll.add(world.company, "stranger", False, URL, world.project)
    with pytest.raises(errors.bad_request.ValidationError):
        bll.add(world.company, "owner", False, "not a repository", world.project)
    assert bll.add(world.company, "owner", False, URL, world.project).id == binding.id


def test_the_badge_does_not_make_a_binding(world):
    from apiserver.bll import repo_access

    row = repo_access.resolve_one(world.company, URL, "owner", world.project, False)
    assert row["state"] == bll.ANONYMOUS and row["binding"] is None and row["can_assign"] is False
    assert RepositoryBinding.objects(company=world.company).count() == 0
    recorded = repo_access.resolve_one(world.company, URL, "owner", world.project, False, record=True)
    assert recorded["binding"] and recorded["can_assign"] is True
    assert repo_access.resolve_one(world.company, URL, "owner", world.project, False)["binding"] == recorded["binding"]


def test_the_requirement_lists_each_repository_of_a_project_once_with_its_tasks(world):
    from apiserver.bll import repo_access
    from apiserver.database.model.task.task import Script, Task

    for index, address in enumerate((URL, URL + ".git", "git@github.com:Acme/Proj.git")):
        Task(id=f"t{index}-{world.company}", company=world.company, user="owner", project=world.project, name="task", type="training",
             script=Script(repository=address), created=datetime.utcnow(), last_update=datetime.utcnow()).save()
    try:
        rows = repo_access.requirement(world.company, "owner", [f"t{i}-{world.company}" for i in range(3)], False)
        assert len(rows) == 1 and len(rows[0]["tasks"]) == 3
        assert rows[0]["state"] == bll.ANONYMOUS and rows[0]["ask"] is True and rows[0]["can_assign"] is True
        assert rows[0]["anonymous"] is True
        assert repo_access.requirement(world.company, "stranger", [f"t0-{world.company}"], False) == []
    finally:
        Task.objects(company=world.company).delete()


def test_another_host_is_reached_by_the_company_only_with_a_stored_credential(world):
    binding = bll.discover(world.company, "https://git.example.org/team/app.git", "owner", world.project)
    source = CodeRepository.objects(id=binding.source).first()
    world.probe.private = True
    assert bll.resolve(source, binding, reach=False).state == bll.UNREACHABLE
    world.monkeypatch.setattr(bll.store, "find_credential", lambda company, repository: SimpleNamespace(host="git.example.org"))
    assert bll.resolve(source, binding, reach=False).state == bll.COMPANY


def test_none_needed_is_not_refused_as_private_when_the_host_could_not_be_asked(world):
    binding = discover(world)
    world.probe.private = None
    with pytest.raises(errors.bad_request.ValidationError, match="try again"):
        bll.set_credential(world.company, "owner", False, binding.id, None, none_needed=True)
