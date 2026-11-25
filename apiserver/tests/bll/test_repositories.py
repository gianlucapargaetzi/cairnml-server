"""Repositories: one record, one credential mechanism. Who sees what, who may assign what, and how one repository is read for a
run in a project by a user (apiserver/bll/repositories)."""

import logging
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from apiserver.apierrors import errors
from apiserver.bll import user_access
from apiserver.bll import access, repositories as bll
from apiserver.database.model.audit import AuditEvent
from apiserver.database.model.auth import AuthUser
from apiserver.database.model.device import Device
from apiserver.database.model.project import Project, ProjectMember, ProjectRole
from apiserver.database.model.repository import Assignment, RegistryEntry, Repository, RepositoryCheck
from apiserver.database.model.share import Share
from apiserver.database.model.user import UserProfile
from apiserver.database.model.user_access import UserAccess
from apiserver.idp import git_credentials as store
from apiserver.idp.github import (
    GitHubApp,
    GitHubError,
    GitHubRefused,
    GitHubUnreachable,
    InstallationToken,
    RepoNotAccessible,
)
from apiserver.tests.db import ensure_db


def _patch(monkeypatch, name, value):
    """The helpers moved to bll/repository_access in the split of bll/repositories: patch both places that may look them up."""
    from apiserver.bll import repository_access

    for module in (bll, repository_access):
        monkeypatch.setattr(module, name, value, raising=False)


ensure_db()
URL = "https://github.com/acme/proj"


@pytest.fixture
def world(monkeypatch):
    """A company with an admin, a project owned by ``owner`` with ``member`` in it, and two outsiders. GitHub is replaced: the host
    says a repository is private, a credential reads every repository, and the company has no App."""
    company = f"c-rep-{uuid4().hex[:8]}"
    now = datetime.utcnow()
    for user, role in (
        ("admin", "admin"),
        ("owner", "user"),
        ("member", "user"),
        ("other", "user"),
        ("stranger", "user"),
    ):
        AuthUser(id=user, name=user.title(), company=company, role=role, created=now).save()
    root = f"p-{company}"
    Project(
        id=root,
        company=company,
        user="owner",
        name="P",
        basename="P",
        created=now,
        last_update=now,
        members=[ProjectMember(user="member", role=ProjectRole.read, added=now, added_by="owner")],
    ).save()
    other_root = f"q-{company}"
    Project(id=other_root, company=company, user="other", name="Q", basename="Q", created=now, last_update=now).save()
    state = SimpleNamespace(
        company=company,
        root=root,
        other_root=other_root,
        private=True,
        reads=True,
        apps=[],
        reach=(None, "no company GitHub App is set up"),
        probes=[],
    )

    def fake_private(address):
        state.probes.append(address)
        return state.private

    def fake_check_reads(entry, repo):
        if not state.reads:
            raise errors.bad_request.ValidationError(f"the GitHub App {entry.label} is not installed on {repo.path}")

    monkeypatch.setattr(store, "key_material", lambda: "test-key-material")
    _patch(monkeypatch, "is_private", fake_private)
    _patch(monkeypatch, "_check_reads", fake_check_reads)
    _patch(monkeypatch, "_company_reach", lambda repo: state.reach)
    monkeypatch.setattr(user_access, "company_apps", lambda company=None: state.apps)
    yield state
    for model in (
        Repository,
        RegistryEntry,
        Assignment,
        RepositoryCheck,
        Share,
        Device,
        UserAccess,
        Project,
        UserProfile,
        AuthUser,
        AuditEvent,
    ):
        model.objects(company=company).delete()


def credential(world, entry_id, user, scope="user", kind="app"):
    UserAccess(
        id=entry_id,
        company=world.company,
        user=user,
        kind=kind,
        app_id="1",
        label=f"cred {entry_id}",
        scope=scope,
        host="github.com",
        path="",
        secret=store.encrypt("pem", store.key_material()),
        created=datetime.utcnow(),
    ).save()
    return entry_id


def use(world, user="owner", url=URL, role="code", project=None):
    return bll.record_use(world.company, url, role, user, project or world.root)


def rows(world, user, is_admin=False, project=None):
    return bll.list_for(world.company, user, is_admin, project)


# --- records ------------------------------------------------------------------------------------------------------------------


def test_every_spelling_of_an_address_is_one_repository(world):
    spellings = [
        URL,
        URL + ".git",
        "git@github.com:Acme/Proj.git",
        "ssh://git@github.com/acme/proj",
        "GIT://GitHub.com/ACME/proj/",
    ]
    ids = {use(world, url=spelling).id for spelling in spellings}
    assert len(ids) == 1 and Repository.objects(company=world.company).count() == 1


def test_a_run_makes_the_record_for_the_owner_of_its_project_and_lists_it_there(world):
    repo = use(world, user="member")  # a member who does not own the project ran it
    assert repo.owner == "owner"
    link = access.find_share(world.company, "repository", repo.id, "projects", world.root)
    assert link is not None and link.inherited and link.granted_by == "owner"


def test_a_run_outside_any_project_makes_it_for_the_user(world):
    repo = bll.record_use(world.company, URL, "code", "stranger", None)
    assert repo.owner == "stranger" and bll.linked_projects(repo) == []


def test_a_repository_used_in_a_second_project_is_listed_there_and_not_made_again(world):
    first = use(world)
    second = use(world, user="other", project=world.other_root, role="dataset")
    assert first.id == second.id and second.owner == "owner"
    assert {s.project for s in bll.linked_projects(first)} == {world.root, world.other_root}
    assert set(Repository.objects(id=first.id).first().roles) == {"code", "dataset"}


def test_an_address_that_names_no_hosted_repository_makes_no_record(world):
    assert bll.record_use(world.company, "/home/me/code", "code", "owner", world.root) is None
    assert Repository.objects(company=world.company).count() == 0


def test_a_dataset_gets_a_unique_reference(world):
    one = use(world, url="https://github.com/acme/cars", role="dataset")
    two = use(world, url="https://github.com/other/cars", role="dataset")
    assert one.reference == "cars" and two.reference == "other/cars"
    assert bll.find_reference(world.company, "cars").id == one.id
    assert bll.find_reference(world.company, "https://github.com/other/cars.git").id == two.id


# --- who sees what ------------------------------------------------------------------------------------------------------------


def test_a_non_shared_repository_is_seen_by_its_owner_and_the_members_of_the_projects_it_is_listed_in(world):
    use(world)
    assert [r["name"] for r in rows(world, "owner")] == ["proj"]
    assert [r["name"] for r in rows(world, "member")] == ["proj"]
    assert rows(world, "stranger") == [] and rows(world, "other") == []
    assert [r["name"] for r in rows(world, "admin", True)] == ["proj"]


def test_listing_it_in_another_project_shows_it_to_that_projects_members(world):
    use(world)
    use(world, user="other", project=world.other_root)
    assert [r["name"] for r in rows(world, "other")] == ["proj"]


def test_a_shared_repository_is_seen_by_every_user(world):
    repo = bll.add(world.company, "admin", True, URL, "dataset", shared=True)
    assert repo.owner == "admin" and bll.is_shared(repo)
    for user in ("owner", "stranger", "other"):
        assert [r["name"] for r in rows(world, user)] == ["proj"]


def test_a_project_filter_lists_only_what_is_listed_in_the_project(world):
    use(world)
    use(world, url="https://github.com/acme/else", user="other", project=world.other_root)
    assert [r["name"] for r in rows(world, "admin", True, world.root)] == ["proj"]


# --- adding, sharing, deleting ------------------------------------------------------------------------------------------------


def test_a_user_adds_only_a_non_shared_repository(world):
    repo = bll.add(world.company, "stranger", False, URL, "dataset")
    assert repo.owner == "stranger" and not bll.is_shared(repo)
    with pytest.raises(errors.forbidden.NoWritePermission):
        bll.add(world.company, "stranger", False, "https://github.com/acme/x", "dataset", shared=True)
    with pytest.raises(errors.bad_request.ValidationError):
        bll.add(world.company, "stranger", False, "not a repository", "dataset")
    with pytest.raises(errors.bad_request.ValidationError):
        bll.add(world.company, "stranger", False, "https://github.com/acme/y", "scripts")


def test_adding_a_repository_somebody_else_lists_privately_is_refused(world):
    use(world)
    with pytest.raises(errors.bad_request.ValidationError, match="already listed"):
        bll.add(world.company, "stranger", False, URL, "dataset")


def test_adding_with_a_project_lists_it_there_for_a_member_only(world):
    repo = bll.add(world.company, "member", False, URL, "code", project_id=world.root)
    assert bll.linked_projects(repo)
    with pytest.raises(errors.forbidden.NoWritePermission):
        bll.add(world.company, "stranger", False, "https://github.com/acme/z", "code", project_id=world.root)


def test_the_owner_makes_a_repository_shared_and_ownership_moves_to_the_admin(world):
    credential(world, "mine", "owner")
    credential(world, "company", "admin", scope="company")
    repo = use(world)
    bll.assign(world.company, "owner", repo.id, "mine")
    world.private = False
    bll.assign(world.company, "member", repo.id, none_needed=True)
    world.private = True
    shared = bll.set_shared(world.company, "owner", False, repo.id, True)
    assert shared.owner == "admin" and bll.is_shared(shared)
    assert not Assignment.objects(repository=repo.id, user="owner")  # the former owner's assignment is dropped
    assert Assignment.objects(repository=repo.id, user="member").first().none_needed  # none needed is kept


def test_making_a_repository_shared_drops_assignments_of_credentials_that_are_not_the_companys(world):
    credential(world, "member-own", "member")
    credential(world, "company", "admin", scope="company")
    repo = use(world)
    bll.assign(world.company, "member", repo.id, "member-own")
    bll.assign(world.company, "admin", repo.id, "company", is_admin=True)
    bll.set_shared(world.company, "admin", True, repo.id, True)
    assert not Assignment.objects(repository=repo.id, user="member")
    assert Assignment.objects(repository=repo.id, user="admin").first().credential == "company"


def test_only_the_owner_or_the_admin_makes_a_repository_shared_and_only_the_admin_undoes_it(world):
    repo = use(world)
    with pytest.raises(errors.forbidden.NoWritePermission):
        bll.set_shared(world.company, "member", False, repo.id, True)
    bll.set_shared(world.company, "owner", False, repo.id, True)
    with pytest.raises(errors.forbidden.NoWritePermission):
        bll.set_shared(world.company, "owner", False, repo.id, False)
    assert not bll.is_shared(bll.set_shared(world.company, "admin", True, repo.id, False))


def test_a_repository_nobody_else_uses_is_deleted_with_everything_on_it(world):
    credential(world, "mine", "owner")
    repo = bll.add(world.company, "owner", False, URL, "dataset")
    bll.assign(world.company, "owner", repo.id, "mine")
    RegistryEntry(id="e1", company=world.company, registry=repo.id, name="cars").save()
    with pytest.raises(errors.bad_request.ValidationError, match="no such repository"):
        bll.delete(world.company, "stranger", False, repo.id)
    bll.delete(world.company, "owner", False, repo.id)
    assert (
        Repository.objects(company=world.company).count() == 0
        and Assignment.objects(company=world.company).count() == 0
    )
    assert (
        RegistryEntry.objects(company=world.company).count() == 0
        and Share.objects(company=world.company, resource_type="repository").count() == 0
    )


def test_unlinking_takes_a_repository_out_of_a_project_and_a_run_lists_it_again(world):
    repo = use(world)
    bll.unlink(world.company, "owner", False, repo.id, world.root)
    assert rows(world, "member") == []
    use(world)
    assert [r["name"] for r in rows(world, "member")] == ["proj"]


# --- assigning ----------------------------------------------------------------------------------------------------------------


def test_a_user_assigns_their_own_credential_and_the_companys_and_never_somebody_elses(world):
    credential(world, "mine", "member")
    credential(world, "theirs", "owner")
    credential(world, "company", "admin", scope="company")
    repo = use(world)
    assert bll.assign(world.company, "member", repo.id, "mine").credential == "mine"
    assert bll.assign(world.company, "member", repo.id, "company").credential == "company"
    with pytest.raises(errors.bad_request.ValidationError, match="not one you may assign"):
        bll.assign(world.company, "member", repo.id, "theirs")


def test_a_shared_repository_takes_company_credentials_only(world):
    credential(world, "mine", "member")
    credential(world, "company", "admin", scope="company")
    repo = bll.add(world.company, "admin", True, URL, "dataset", shared=True)
    with pytest.raises(errors.bad_request.ValidationError, match="company credential"):
        bll.assign(world.company, "member", repo.id, "mine")
    assert bll.assign(world.company, "member", repo.id, "company").credential == "company"
    assert [c["id"] for c in bll.view(world.company, "member", False, repo)["credentials"]] == ["company"]


def test_a_credential_that_cannot_read_the_repository_is_refused(world):
    credential(world, "mine", "member")
    repo = use(world)
    world.reads = False
    with pytest.raises(errors.bad_request.ValidationError, match="not installed"):
        bll.assign(world.company, "member", repo.id, "mine")
    assert Assignment.objects(company=world.company).count() == 0


def test_none_needed_needs_a_repository_the_host_clones_without_a_credential(world):
    repo = use(world)
    with pytest.raises(errors.bad_request.ValidationError, match="cannot be cloned"):
        bll.assign(world.company, "member", repo.id, none_needed=True)
    world.private = False
    assert bll.assign(world.company, "member", repo.id, none_needed=True).none_needed


def test_a_user_who_does_not_see_a_repository_assigns_nothing_to_it(world):
    credential(world, "mine", "stranger")
    repo = use(world)
    with pytest.raises(errors.bad_request.ValidationError, match="no such repository"):
        bll.assign(world.company, "stranger", repo.id, "mine")


def test_assigning_one_thing_and_none_needed_together_is_refused_and_nothing_clears_the_assignment(world):
    credential(world, "mine", "member")
    repo = use(world)
    with pytest.raises(errors.bad_request.ValidationError, match="not both"):
        bll.assign(world.company, "member", repo.id, "mine", none_needed=True)
    bll.assign(world.company, "member", repo.id, "mine")
    assert bll.assign(world.company, "member", repo.id) is None
    assert Assignment.objects(company=world.company).count() == 0


def test_only_the_users_own_assignment_is_changed_by_unassign(world):
    credential(world, "a", "owner")
    credential(world, "b", "member")
    repo = use(world)
    bll.assign(world.company, "owner", repo.id, "a")
    bll.assign(world.company, "member", repo.id, "b")
    bll.unassign(world.company, "member", repo.id)
    assert [x.user for x in Assignment.objects(repository=repo.id)] == ["owner"]


def test_deleting_a_credential_removes_the_assignments_of_it(world):
    from apiserver.bll import user_access

    credential(world, "mine", "member")
    repo = use(world)
    bll.assign(world.company, "member", repo.id, "mine")
    user_access.delete(world.company, "member", "mine")
    assert Assignment.objects(repository=repo.id).count() == 0


# --- how a repository is read ---------------------------------------------------------------------------------------------------


def resolve(world, repo, user, project="root"):
    root = world.root if project == "root" else project
    return bll.resolve(repo, root, user, now=datetime.utcnow())


def test_the_project_owners_assignment_is_used_first(world):
    credential(world, "owner-cred", "owner")
    credential(world, "member-cred", "member")
    repo = use(world)
    bll.assign(world.company, "owner", repo.id, "owner-cred")
    bll.assign(world.company, "member", repo.id, "member-cred")
    answer = resolve(world, repo, "member")
    assert answer.state == bll.ASSIGNED and answer.entry.id == "owner-cred" and not answer.warning


def test_the_runners_own_assignment_is_a_fallback_with_a_warning_when_the_owner_has_none(world):
    credential(world, "member-cred", "member")
    repo = use(world)
    bll.assign(world.company, "member", repo.id, "member-cred")
    answer = resolve(world, repo, "member")
    assert answer.state == bll.ASSIGNED and answer.entry.id == "member-cred" and answer.warning
    assert "assign one for the project" in answer.message


def test_the_runners_assignment_is_not_used_for_somebody_elses_run(world):
    credential(world, "member-cred", "member")
    repo = use(world)
    bll.assign(world.company, "member", repo.id, "member-cred")
    assert resolve(world, repo, "owner").state == bll.UNREACHABLE


def test_the_record_owners_credential_is_not_a_fallback_for_another_project(world):
    credential(world, "owner-cred", "owner")
    repo = use(world)
    use(world, user="other", project=world.other_root)
    bll.assign(world.company, "owner", repo.id, "owner-cred")
    assert bll.resolve(repo, world.other_root, "other").state == bll.UNREACHABLE


def test_a_credential_the_owner_assigned_that_cannot_be_used_is_not_replaced_by_a_fallback(world):
    credential(world, "owner-cred", "owner")
    credential(world, "member-cred", "member")
    repo = use(world)
    bll.assign(world.company, "owner", repo.id, "owner-cred")
    bll.assign(world.company, "member", repo.id, "member-cred")
    UserAccess.objects(id="owner-cred").delete()
    # deleting through the model leaves the assignment (as a credential of an owner that is disabled does)
    answer = resolve(world, repo, "member")
    assert answer.state == bll.UNREACHABLE and "no longer exists" in answer.message


def test_a_repository_the_host_clones_without_a_credential_needs_none(world):
    world.private = False
    repo = use(world)
    assert resolve(world, repo, "member").state == bll.ANONYMOUS


def test_none_needed_is_read_without_a_credential(world):
    world.private = False
    repo = use(world)
    bll.assign(world.company, "owner", repo.id, none_needed=True)
    assert resolve(world, repo, "member").state == bll.NONE_NEEDED


def test_the_company_app_reads_a_private_repository_nobody_assigned_with_a_warning(world):
    repo = use(world)
    world.reach = (SimpleNamespace(label="App A", id="app-a"), None)
    answer = resolve(world, repo, "member")
    assert answer.state == bll.COMPANY and answer.warning and "App A" in answer.message


def test_a_private_repository_nobody_reaches_is_not_readable(world):
    repo = use(world)
    answer = resolve(world, repo, "member")
    assert answer.state == bll.UNREACHABLE and "no credential is assigned" in answer.message
    # the text must not suggest that the company App is the only way in: the user's own credential is enough
    assert "Assigning your own credential is enough" in answer.message
    assert "no company GitHub App is set up" in answer.message  # the reason stays, as the fallback's explanation


def test_a_host_that_cannot_be_asked_is_unknown_and_never_refused(world):
    world.private = None
    repo = use(world)
    answer = resolve(world, repo, "member")
    assert answer.state == bll.UNKNOWN
    assert bll.token_for(repo, answer)["token"] is None


def test_the_answer_of_the_host_is_kept_and_asked_again_when_forced(world):
    world.private = False
    repo = use(world)
    resolve(world, repo, "owner")
    resolve(world, repo, "owner")
    assert len(world.probes) == 1
    bll.anonymous(repo, force=True)
    assert len(world.probes) == 2


# --- tokens -------------------------------------------------------------------------------------------------------------------


def test_a_public_repository_gives_no_token_and_a_write_is_refused_without_a_github_app(world):
    world.private = False
    repo = use(world)
    answer = bll.token_for_person(world.company, "owner", False, repo)
    assert answer["token"] is None and answer["url"] == URL
    with pytest.raises(errors.bad_request.ValidationError, match="to write"):
        bll.token_for_person(world.company, "owner", False, repo, write=True)


def test_only_the_owner_and_the_admin_write(world):
    world.private = False
    repo = use(world)
    with pytest.raises(errors.forbidden.NoWritePermission):
        bll.token_for_person(world.company, "member", False, repo, write=True)


def test_a_job_reads_with_the_project_owners_credential(world, monkeypatch):
    credential(world, "owner-cred", "owner")
    repo = use(world)
    bll.assign(world.company, "owner", repo.id, "owner-cred")
    _patch(
        monkeypatch,
        "_token_of",
        lambda entry, r, permissions=None: {"token": f"tok-{entry.id}", "expires_at": None, "username": "x"},
    )
    answer, resolution = bll.token_for_job(world.company, repo, world.root, "member")
    assert answer["token"] == "tok-owner-cred" and resolution.state == bll.ASSIGNED


def test_an_unreadable_repository_refuses_the_job_with_the_reason(world):
    repo = use(world)
    with pytest.raises(errors.bad_request.ValidationError, match="no credential is assigned"):
        bll.token_for_job(world.company, repo, world.root, "member")


# --- what the list shows -----------------------------------------------------------------------------------------------------


def test_the_list_shows_every_assignment_and_only_the_users_own_are_editable(world):
    credential(world, "owner-cred", "owner")
    credential(world, "member-cred", "member")
    repo = use(world)
    bll.assign(world.company, "owner", repo.id, "owner-cred")
    bll.assign(world.company, "member", repo.id, "member-cred")
    row = rows(world, "member")[0]
    by_user = {a["user"]: a for a in row["assignments"]}
    assert by_user["member"]["mine"] and by_user["member"]["can_edit"]
    assert not by_user["owner"]["mine"] and not by_user["owner"]["can_edit"]
    assert by_user["owner"]["credential_name"] == "cred owner-cred" and [
        (u["project"], u["as"]) for u in by_user["owner"]["used_in"]
    ] == [(world.root, "owner")]
    assert row["relation"] == "inherited" and rows(world, "owner")[0]["relation"] == "own"
    assert "secret" not in str(row)


def test_the_list_says_what_the_user_may_do(world):
    repo = use(world)
    owner_row, member_row = rows(world, "owner")[0], rows(world, "member")[0]
    assert owner_row["can_share"] and not member_row["can_share"] and repo.id == owner_row["id"]
    assert not owner_row["can_delete"] and "used in P" in owner_row["delete_blocked"].replace(
        "It is ", ""
    )  # a project lists it
    bll.unlink(world.company, "owner", False, repo.id, world.root)
    assert rows(world, "owner")[0]["can_delete"] and rows(world, "owner")[0]["delete_blocked"] is None


def test_a_shared_row_is_owned_by_the_company_and_deleted_by_the_admin_only(world):
    bll.add(world.company, "admin", True, URL, "dataset", shared=True)
    row = rows(world, "stranger")[0]
    assert row["shared"] and row["owner_name"] == "Company" and not row["can_delete"] and not row["can_share"]
    assert "admin" in row["delete_blocked"]
    assert rows(world, "admin", True)[0]["can_delete"] and rows(world, "admin", True)[0]["can_share"]


def shown(world, user, repo):
    """The repository as the user's list shows it now."""
    return bll.view(world.company, user, False, bll.get(world.company, repo.id))


def test_checking_stores_what_it_found(world):
    credential(world, "mine", "member")
    repo = use(world)
    bll.assign(world.company, "member", repo.id, "mine")
    bll.check(world.company, "member", False, repo.id)
    assert shown(world, "member", repo)["state"] == "ok"
    world.reads = False
    bll.check(world.company, "member", False, repo.id)
    found = shown(world, "member", repo)
    assert found["state"] == "no_access" and "not installed" in found["state_message"] and found["last_checked"]


def _member_reads_owner_does_not(world, monkeypatch):
    """The member's credential reads the repository, the owner's does not."""
    credential(world, "good", "member")
    credential(world, "bad", "owner")
    repo = use(world)
    bll.assign(world.company, "member", repo.id, "good")
    bll.assign(world.company, "owner", repo.id, "bad")

    def check_reads(entry, repo):
        if entry.id == "bad":
            raise errors.bad_request.ValidationError("the GitHub App cred bad is not installed on acme/proj")

    _patch(monkeypatch, "_check_reads", check_reads)
    return repo


def test_a_check_by_one_user_does_not_change_what_another_user_sees(world, monkeypatch):
    # the test of the issue, as it was written there (the helper above does the same setup for the tests that follow)
    credential(world, "good", "member")
    credential(world, "bad", "owner")
    repo = use(world)
    bll.assign(world.company, "member", repo.id, "good")
    bll.assign(world.company, "owner", repo.id, "bad")

    def check_reads(entry, repo):
        if entry.id == "bad":
            raise errors.bad_request.ValidationError("the GitHub App cred bad is not installed on acme/proj")

    _patch(monkeypatch, "_check_reads", check_reads)
    bll.check(world.company, "member", False, repo.id)
    bll.check(world.company, "owner", False, repo.id)
    assert bll.view(world.company, "member", False, bll.get(world.company, repo.id))["state"] == "ok"


def test_checking_shows_the_callers_own_result(world, monkeypatch):
    repo = _member_reads_owner_does_not(world, monkeypatch)
    bll.check(world.company, "owner", False, repo.id)
    owner = shown(world, "owner", repo)
    assert owner["state"] == "no_access" and "not installed" in owner["state_message"] and owner["last_checked"]
    assert [a["readable"] for a in owner["assignments"] if a["mine"]] == [
        False
    ]  # the closed lock is on the owner's own row
    member = shown(world, "member", repo)  # has not checked: what a repository nobody checked shows
    assert member["state"] == "ok" and member["state_message"] == "" and member["last_checked"] is None
    assert all(a["readable"] for a in member["assignments"])
    shared = Repository.objects.get(id=repo.id)  # the record everybody shares was not written
    assert shared.state == "ok" and shared.state_message == "" and shared.last_checked is None
    bll.check(world.company, "member", False, repo.id)
    member = shown(world, "member", repo)
    assert member["state"] == "ok" and member["last_checked"]
    assert shown(world, "owner", repo)["state"] == "no_access"  # and the owner's result is not undone


def test_checking_keeps_the_anonymous_answer_on_the_repository(world):
    repo = use(world)
    world.private = True
    bll.check(world.company, "owner", False, repo.id)
    first = Repository.objects.get(id=repo.id)
    assert first.anonymous is False and first.anonymous_checked
    shared = (first.state, first.state_message, first.last_checked, first.updated)
    world.private = False  # served anonymously now, the kept answer is still fresh
    bll.check(world.company, "member", False, repo.id)
    second = Repository.objects.get(id=repo.id)
    assert second.anonymous is True and second.anonymous_checked > first.anonymous_checked
    assert (second.state, second.state_message, second.last_checked, second.updated) == shared


def test_checking_without_an_assignment_shows_the_callers_own_result(world):
    credential(world, "good", "member")
    repo = use(world)
    bll.assign(world.company, "member", repo.id, "good")
    bll.check(world.company, "owner", False, repo.id)  # the owner has no assignment: private, nobody reads it for them
    owner = shown(world, "owner", repo)
    assert (
        owner["state"] == "no_access"
        and "no credential is assigned" in owner["state_message"]
        and owner["last_checked"]
    )
    assert shown(world, "member", repo)["state"] == "ok"


def test_a_registry_that_cannot_be_read_stays_visible_after_a_check(world):
    credential(world, "good", "member")
    repo = use(world, role="registry")
    bll.assign(world.company, "member", repo.id, "good")
    Repository.objects(id=repo.id).update(state="no_access", state_message="the registry could not be read: refused")
    bll.check(
        world.company, "member", False, repo.id
    )  # the member's own reading is fine, the registry is still unreadable
    row = shown(world, "member", repo)
    assert row["state"] == "no_access" and "registry could not be read" in row["state_message"]
    assert Repository.objects.get(id=repo.id).state_message.startswith("the registry could not be read")


def test_a_legacy_shared_state_does_not_hide_the_callers_own_result(world):
    credential(world, "good", "member")
    repo = use(world)
    bll.assign(world.company, "member", repo.id, "good")
    Repository.objects(id=repo.id).update(state="no_access", state_message="left by a check of somebody else")
    assert shown(world, "member", repo)["state"] == "no_access"  # nobody of theirs checked: what they see today
    bll.check(world.company, "member", False, repo.id)
    row = shown(world, "member", repo)
    assert row["state"] == "ok" and row["state_message"] == ""


def test_stored_checks_go_with_their_repository(world):
    credential(world, "mine", "owner")
    repo = bll.add(world.company, "owner", False, URL, "dataset")
    bll.assign(world.company, "owner", repo.id, "mine")
    bll.check(world.company, "owner", False, repo.id)
    assert RepositoryCheck.objects(repository=repo.id).count() == 1
    bll.delete(world.company, "owner", False, repo.id)
    assert RepositoryCheck.objects(company=world.company).count() == 0


def test_deleting_for_one_user_drops_only_their_stored_check(world):
    credential(world, "mine", "owner")
    repo = bll.add(world.company, "owner", False, URL, "dataset")
    bll.assign(world.company, "owner", repo.id, "mine")
    Assignment(id="a-member", company=world.company, repository=repo.id, user="member", credential="x").save()
    RepositoryCheck(id="k-member", company=world.company, repository=repo.id, user="member", state="ok").save()
    bll.check(world.company, "owner", False, repo.id)
    assert RepositoryCheck.objects(repository=repo.id, user="owner").count() == 1
    bll.delete(world.company, "owner", False, repo.id)
    assert RepositoryCheck.objects(repository=repo.id, user="owner").count() == 0
    assert RepositoryCheck.objects(repository=repo.id, user="member").count() == 1


@pytest.mark.parametrize("assigned", [True, False])
def test_a_user_who_loses_the_repository_loses_the_stored_check(world, assigned):
    credential(world, "member-cred", "member")
    repo = use(world)
    if assigned:
        bll.assign(world.company, "member", repo.id, "member-cred")
    bll.check(world.company, "owner", False, repo.id)
    bll.check(world.company, "member", False, repo.id)
    assert RepositoryCheck.objects(repository=repo.id).count() == 2
    Project.objects(id=world.root).update_one(pull__members__user="member")
    bll.prune_assignments(world.company, user="member")
    assert RepositoryCheck.objects(repository=repo.id, user="member").count() == 0
    assert RepositoryCheck.objects(repository=repo.id, user="owner").count() == 1


def test_the_owner_of_a_project_changing_relinks_its_repositories(world):
    repo = use(world)
    bll.regrant_project_links(world.company, world.root, "member", actor="admin")
    link = access.find_share(world.company, "repository", repo.id, "projects", world.root)
    assert link is not None and link.granted_by == "member"


def test_a_project_whose_owner_has_no_assignment_is_a_row_of_the_owner_without_a_credential(world):
    use(world)
    row = rows(world, "member")[0]
    assert [(a["user_name"], a["credential_name"], a["readable"], a["can_edit"]) for a in row["assignments"]] == [
        ("Owner", "", False, False)
    ]
    assert [(u["project"], u["as"], u["member"]) for u in row["assignments"][0]["used_in"]] == [
        (world.root, "owner", True)
    ]
    assert rows(world, "owner")[0]["assignments"][0]["can_edit"]  # the owner may assign from that row


def test_the_assignment_that_reads_for_a_job_is_marked_used(world, monkeypatch):
    _patch(
        monkeypatch,
        "_token_of",
        lambda entry, r, permissions=None: {"token": "tok", "expires_at": None, "username": "x"},
    )
    credential(world, "owner-cred", "owner")
    repo = use(world)
    bll.assign(world.company, "owner", repo.id, "owner-cred")
    assert rows(world, "owner")[0]["assignments"][0]["last_used"] is None
    bll.token_for_job(world.company, repo, world.root, "member")
    assert rows(world, "owner")[0]["assignments"][0]["last_used"] is not None


# --- sharing needs the company credential, confirmed first --------------------------------------------------------------------------


def test_sharing_with_confirmation_needs_a_company_credential_that_reads_it_and_assigns_it_to_the_admin(world):
    credential(world, "company", "admin", scope="company")
    credential(world, "member-own", "member")
    repo = use(world)
    with pytest.raises(errors.bad_request.ValidationError, match="choose the company credential"):
        bll.set_shared(world.company, "owner", False, repo.id, True, confirm=True)
    with pytest.raises(errors.bad_request.ValidationError, match="company credential"):
        bll.set_shared(world.company, "owner", False, repo.id, True, credential="member-own", confirm=True)
    world.reads = False
    with pytest.raises(errors.bad_request.ValidationError, match="not installed"):
        bll.set_shared(world.company, "owner", False, repo.id, True, credential="company", confirm=True)
    assert not bll.is_shared(bll.get(world.company, repo.id))  # nothing changed when it was refused
    world.reads = True
    bll.set_shared(world.company, "owner", False, repo.id, True, credential="company", confirm=True)
    assert bll.is_shared(bll.get(world.company, repo.id))
    assert Assignment.objects(repository=repo.id, user="admin").first().credential == "company"


def test_a_short_id_names_the_repository_when_it_is_unique(world):
    repo = use(world)
    assert bll.find_reference(world.company, repo.id[:8]).id == repo.id
    assert bll.find_reference(world.company, "zzzzzzzz") is None


def test_a_shared_repository_is_read_with_the_company_credential_assigned_when_it_was_shared(world):
    credential(world, "company", "admin", scope="company")
    repo = use(world)
    bll.set_shared(world.company, "owner", False, repo.id, True, credential="company", confirm=True)
    for user in ("owner", "member", "stranger"):
        resolution = bll.resolve(bll.get(world.company, repo.id), None, user)
        assert resolution.state == bll.ASSIGNED and resolution.entry.id == "company" and not resolution.warning


def test_a_project_of_a_shared_repository_reads_with_the_company_credential_not_with_nothing(world):
    credential(world, "company", "admin", scope="company")
    repo = use(world)
    bll.set_shared(world.company, "owner", False, repo.id, True, credential="company", confirm=True)
    row = rows(world, "member")[0]
    reading = [a for a in row["assignments"] if a["user"] == "owner"]
    assert [(a["credential_name"], a["readable"], a["can_edit"], a["company"]) for a in reading] == [
        ("cred company", True, False, True)
    ]


# --- deleting: for the user who deletes it, never while a project depends on it ---------------------------------------------------


def test_a_repository_a_project_lists_cannot_be_deleted_and_the_reason_is_given(world):
    repo = use(world)
    with pytest.raises(errors.bad_request.ValidationError, match="project depends on it"):
        bll.delete(world.company, "owner", False, repo.id)
    row = rows(world, "owner")[0]
    assert row["can_delete"] is False and "P" in row["delete_blocked"]
    assert rows(world, "stranger") == []  # a user who cannot see it learns nothing of the projects
    with pytest.raises(errors.bad_request.ValidationError, match="project depends on it"):
        bll.delete(world.company, "admin", True, repo.id)  # not even the admin: the project depends on it


def test_deleting_what_others_still_hold_removes_it_for_the_user_only(world):
    credential(world, "mine", "owner")
    repo = bll.add(world.company, "owner", False, URL, "dataset")
    bll.assign(world.company, "owner", repo.id, "mine")
    Assignment(
        id="a-member", company=world.company, repository=repo.id, user="member", credential="x"
    ).save()  # someone else holds one
    bll.delete(world.company, "owner", False, repo.id)
    assert Repository.objects(company=world.company).count() == 1  # the record stays
    assert not Assignment.objects(repository=repo.id, user="owner") and Assignment.objects(
        repository=repo.id, user="member"
    )
    assert rows(world, "owner") == []  # gone for the one who deleted it
    again = bll.add(world.company, "owner", False, URL, "dataset")  # listing it again brings it back
    assert again.id == repo.id and [r["name"] for r in rows(world, "owner")] == ["proj"]


def test_a_project_that_lists_a_repository_shows_it_again_to_a_user_who_deleted_it(world):
    repo = bll.add(world.company, "owner", False, URL, "dataset")
    Assignment(id="a-member", company=world.company, repository=repo.id, user="member", credential="x").save()
    bll.delete(world.company, "owner", False, repo.id)
    assert rows(world, "owner") == []
    use(world)  # a run in the owner's project lists it
    assert [r["name"] for r in rows(world, "owner")] == ["proj"]


def test_only_the_admin_deletes_a_shared_repository(world):
    repo = bll.add(world.company, "admin", True, URL, "dataset", shared=True)
    with pytest.raises(errors.bad_request.ValidationError, match="admin only"):
        bll.delete(world.company, "owner", False, repo.id)
    bll.delete(world.company, "admin", True, repo.id)
    assert Repository.objects(company=world.company).count() == 0


# --- losing the access to a project takes the credential entry with it -----------------------------------------------------------


def test_a_user_who_loses_the_project_loses_the_repository_and_the_credential_assigned_to_it(world):
    credential(world, "member-cred", "member")
    repo = use(world)
    bll.assign(world.company, "member", repo.id, "member-cred")
    assert [r["name"] for r in rows(world, "member")] == ["proj"]
    Project.objects(id=world.root).update_one(pull__members__user="member")  # the member is removed from the project
    assert bll.prune_assignments(world.company, user="member") == 1
    assert not Assignment.objects(repository=repo.id, user="member") and rows(world, "member") == []
    assert [r["name"] for r in rows(world, "owner")] == ["proj"]  # the others keep it


def test_a_user_who_still_uses_the_repository_in_a_project_of_their_own_keeps_the_credential(world):
    credential(world, "other-cred", "other")
    repo = use(world, user="other", project=world.other_root)  # the record is the owner's; Q lists it too
    use(world)
    bll.assign(world.company, "other", repo.id, "other-cred")
    bll.unlink(world.company, "owner", False, repo.id, world.root)  # taken out of P: Q still lists it
    assert Assignment.objects(repository=repo.id, user="other") and [r["name"] for r in rows(world, "other")] == [
        "proj"
    ]


def test_taking_a_repository_out_of_a_project_drops_the_assignments_of_those_who_only_saw_it_there(world):
    credential(world, "member-cred", "member")
    repo = use(world)
    bll.assign(world.company, "member", repo.id, "member-cred")
    bll.unlink(world.company, "owner", False, repo.id, world.root)
    assert not Assignment.objects(repository=repo.id, user="member") and rows(world, "member") == []


# --- who sees what, and the credentials a record may be read with ------------------------------------------------------------------


def test_everyone_sees_only_their_own_records_the_ones_of_their_projects_and_the_shared_ones(world):
    mine = use(world, url="https://github.com/acme/mine", user="owner")
    theirs = use(world, url="https://github.com/acme/theirs", user="other", project=world.other_root)
    bll.add(world.company, "admin", True, "https://github.com/acme/everyone", "dataset", shared=True)
    names = lambda user: sorted(r["name"] for r in rows(world, user))
    assert names("owner") == ["everyone", "mine"] and names("member") == ["everyone", "mine"]
    assert names("other") == ["everyone", "theirs"] and names("stranger") == ["everyone"]
    assert {mine.id, theirs.id}.isdisjoint({r["id"] for r in rows(world, "stranger")})


def test_the_company_github_app_can_be_assigned_to_a_non_shared_record_and_to_a_shared_one(world):
    credential(world, "company-app", "admin", scope="company")
    repo = use(world)
    bll.assign(world.company, "owner", repo.id, "company-app")
    assert Assignment.objects(repository=repo.id, user="owner").first().credential == "company-app"
    assert {"company-app"} <= {c["id"] for c in rows(world, "owner")[0]["credentials"] if c["company"]}
    shared = bll.add(world.company, "admin", True, "https://github.com/acme/all", "dataset", shared=True)
    bll.assign(world.company, "admin", shared.id, "company-app", is_admin=True)
    assert Assignment.objects(repository=shared.id, user="admin").first().credential == "company-app"


# --- a missing credential is not a missing access -----------------------------------------------------------------------------------


def test_a_public_repository_without_credentials_is_one_empty_row_with_all_its_projects_and_an_open_lock(world):
    world.private = False
    repo = use(world)
    use(world, user="other", project=world.other_root)
    row = rows(world, "member")[0]
    assert len(row["assignments"]) == 1
    empty = row["assignments"][0]
    assert empty["credential_name"] == "" and empty["readable"] is True
    assert sorted(u["project"] for u in empty["used_in"]) == sorted([world.root, world.other_root])
    assert row["access"]["readable"] is True and repo.id == row["id"]


def test_a_private_repository_without_credentials_is_one_empty_row_with_a_closed_lock(world):
    use(world)
    use(world, user="other", project=world.other_root)
    row = rows(world, "owner")[0]
    assert [(a["credential_name"], a["readable"]) for a in row["assignments"]] == [("", False)]
    assert len(row["assignments"][0]["used_in"]) == 2 and row["access"]["readable"] is False


def test_the_empty_row_is_split_when_the_projects_read_differently(world):
    credential(world, "owner-cred", "owner")
    repo = use(world)  # P: its owner assigns a credential below
    use(world, user="other", project=world.other_root)  # Q: nobody assigns one, and it is private
    bll.assign(world.company, "owner", repo.id, "owner-cred")
    row = rows(world, "owner")[0]
    kinds = sorted((a["credential_name"], a["readable"]) for a in row["assignments"])
    assert kinds == [("", False), ("cred owner-cred", True)]


def test_a_credential_that_reads_makes_the_record_readable(world):
    credential(world, "mine", "owner")
    repo = use(world)
    bll.assign(world.company, "owner", repo.id, "mine")
    row = rows(world, "owner")[0]
    assert row["access"]["readable"] is True and [a["readable"] for a in row["assignments"]] == [True]


def test_outside_a_project_the_users_own_credential_is_no_fallback_and_carries_no_warning(world):
    credential(world, "member-cred", "member")
    repo = use(world)
    bll.assign(world.company, "member", repo.id, "member-cred")
    outside = bll.resolve(repo, None, "member")
    inside = bll.resolve(repo, world.root, "member")  # the project's owner has none: a fallback, with a warning
    assert outside.state == inside.state == bll.ASSIGNED
    assert not outside.warning and "project" not in outside.message
    assert inside.warning and "project" in inside.message


def test_a_write_token_refused_by_github_says_the_app_needs_write_access(world):
    from apiserver.idp.github import GitHubError

    class App:
        def installation_token(self, repo, permissions=None):
            raise GitHubError("token request failed: 422")

    repo = use(world)
    with pytest.raises(errors.bad_request.ValidationError, match="Contents and Workflows: Read and write"):
        bll._app_token(App(), repo, "the GitHub App x", bll.WRITE_PERMISSIONS)
    with pytest.raises(errors.bad_request.ValidationError) as read_refused:  # a read token has no such hint
        bll._app_token(App(), repo, "the GitHub App x")
    assert "write" not in read_refused.value.msg


class FakeApp:
    """A GitHub App whose token request raises the exceptions given, one per call, and then issues a token."""

    def __init__(self, *failures):
        self.failures = list(failures)
        self.calls = 0

    def installation_token(self, repo, permissions=None):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return InstallationToken("ghs_x", datetime(2099, 1, 1))


class GitHubHttp:
    """The HTTP session under a real GitHubApp: the installation lookup and the access-token POST of acme/proj. The token POST
    raises the given errors, one per call, before it answers with a token."""

    INSTALL = "/repos/acme/proj/installation"
    TOKEN = "/app/installations/77/access_tokens"

    def __init__(self, *token_failures):
        self.token_failures = list(token_failures)
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url))
        if url.endswith(self.INSTALL):
            return SimpleNamespace(status_code=200, json=lambda: {"id": 77})
        if url.endswith(self.TOKEN):
            if self.token_failures:
                raise self.token_failures.pop(0)
            return SimpleNamespace(
                status_code=201, json=lambda: {"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"}
            )
        raise AssertionError(f"unexpected {method} {url}")

    def paths(self, suffix):
        return [url for _method, url in self.requests if url.endswith(suffix)]


def _github_app(http):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return GitHubApp("123", pem, "https://api.github.com", http=http, clock=lambda: 1000.0)


def test_a_transient_github_failure_is_retried_once(world, monkeypatch):
    pauses = []
    _patch(monkeypatch, "_pause", pauses.append)
    http = GitHubHttp(requests.ConnectionError("connection reset"))  # the real client path, the token POST fails once
    repo = use(world)
    answer = bll._app_token(_github_app(http), repo, "the GitHub App x")
    assert answer["token"] == "ghs_x"
    assert len(http.paths(GitHubHttp.TOKEN)) == 2
    assert len(http.paths(GitHubHttp.INSTALL)) == 1  # the installation id is kept by the App for the second attempt
    assert pauses == [1]


@pytest.mark.parametrize(
    "refusal",
    [
        GitHubError("token request failed: 500"),
        GitHubRefused("GitHub did not accept the App ID or its key (401)"),
        RepoNotAccessible("acme/proj"),
    ],
)
def test_a_refusal_by_github_is_not_retried(world, monkeypatch, refusal):
    pauses = []
    _patch(monkeypatch, "_pause", pauses.append)
    app = FakeApp(refusal)
    repo = use(world)
    with pytest.raises(errors.bad_request.ValidationError):
        bll._app_token(app, repo, "the GitHub App x")
    assert app.calls == 1 and pauses == []


def test_a_timeout_is_not_retried(world, monkeypatch):
    pauses = []
    _patch(monkeypatch, "_pause", pauses.append)
    http = GitHubHttp(requests.ReadTimeout("read timed out"))
    repo = use(world)
    with pytest.raises(errors.bad_request.ValidationError):
        bll._app_token(_github_app(http), repo, "the GitHub App x")
    assert len(http.paths(GitHubHttp.TOKEN)) == 1 and pauses == []


def _unreachable(cause="connection reset"):
    """What the GitHub client raises for a network failure: the requests error is kept as the cause."""
    error = GitHubUnreachable("GitHub transport error")
    error.__cause__ = requests.ConnectionError(cause)
    return error


def test_an_unreachable_github_says_so_after_three_attempts(world, monkeypatch):
    pauses = []
    _patch(monkeypatch, "_pause", pauses.append)
    app = FakeApp(_unreachable(), _unreachable(), _unreachable())
    repo = use(world)
    with pytest.raises(errors.bad_request.ValidationError) as caught:
        bll._app_token(app, repo, "the GitHub App x")
    assert caught.value.code == 400
    assert "GitHub could not be reached" in caught.value.msg and "try again" in caught.value.msg
    assert app.calls == 3 and pauses == [1, 2]


def test_each_failed_attempt_logs_the_cause(world, monkeypatch, caplog):
    _patch(monkeypatch, "_pause", lambda seconds: None)
    app = FakeApp(_unreachable("connection reset"), _unreachable("name resolution failed"), _unreachable("refused"))
    repo = use(world)
    with caplog.at_level(logging.WARNING, logger="apiserver.bll.repository_access"):
        with pytest.raises(errors.bad_request.ValidationError):
            bll._app_token(app, repo, "the GitHub App x")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "could not be reached" in r.getMessage()]
    assert len(warnings) == 3
    assert "attempt 3 of 3" in warnings[-1].getMessage()
    assert "ConnectionError: connection reset" in warnings[0].getMessage()
    assert "name resolution failed" in warnings[1].getMessage()


# --- no record is left pointing at a deleted one ----------------------------------------------------------------------------


def test_deleting_a_credential_leaves_no_orphan(world):
    from apiserver.bll import user_access
    from apiserver.bll.integrity import find_orphans

    credential(world, "mine", "member")
    repo = use(world)
    bll.assign(world.company, "member", repo.id, "mine")
    user_access.delete(world.company, "member", "mine")
    assert find_orphans(world.company) == {}


def test_deleting_a_repository_leaves_no_orphan(world):
    from apiserver.bll.integrity import find_orphans

    credential(world, "mine", "owner")
    repo = bll.add(world.company, "owner", False, URL, "dataset")  # used by nobody else, so it may be deleted
    bll.assign(world.company, "owner", repo.id, "mine")
    bll.delete(world.company, "owner", False, repo.id)
    assert find_orphans(world.company) == {}


def test_deleting_a_project_leaves_no_orphan(world):
    from apiserver.bll.integrity import find_orphans
    from apiserver.bll.project.project_cleanup import delete_project

    credential(world, "mine", "owner")
    repo = use(world)  # the run lists the repository in the project: a share of the project
    bll.assign(world.company, "owner", repo.id, "mine")
    delete_project(
        world.company, "owner", world.root, force=True, delete_contents=False, delete_external_artifacts=False
    )
    assert not Project.objects(id=world.root).first()
    assert find_orphans(world.company) == {}


def delete_user(world, user, actor="root"):
    """The reachable user delete: users.delete (system and root only) calls UserBLL.delete."""
    from apiserver.bll.user import UserBLL
    from apiserver.database.model.user import UserProfile

    if not UserProfile.objects(id=user).first():
        UserProfile(id=user, company=world.company, name=user.title(), created=datetime.utcnow()).save()
    UserBLL.delete(user, actor=actor)


def test_deleting_a_user_leaves_no_orphan(world):
    from apiserver.bll.integrity import find_orphans
    from apiserver.database.model.user import UserProfile

    credential(world, "mine", "member")
    repo = use(world)
    bll.assign(world.company, "member", repo.id, "mine")
    # the user also has a share granted to them, a device and a repository check
    now = datetime.utcnow()
    Share(
        id=uuid4().hex,
        company=world.company,
        resource_type="queue",
        resource_id="queue-1",
        scope="user",
        user="member",
        level="read",
        granted_by="admin",
        created=now,
    ).save()
    Device(id=uuid4().hex, user="member", company=world.company, public_key="pem", created=now).save()
    RepositoryCheck(id=uuid4().hex, company=world.company, repository=repo.id, user="member", checked=now).save()
    delete_user(world, "member")
    assert not UserProfile.objects(id="member").first()
    assert not UserAccess.objects(company=world.company, user="member")
    assert not Assignment.objects(company=world.company, user="member")
    # the login record goes with the user (users.delete removes it), so nothing may point at the user
    assert AuthUser.objects(id="member").first() is None
    assert find_orphans(world.company) == {}


def test_a_live_device_of_a_missing_user_is_an_orphan_and_a_revoked_one_is_not(world):
    """A revoked device stays as audit history, so it may name a user who is gone; a live one may not."""
    from apiserver.bll.integrity import find_orphans

    live = Device(id=uuid4().hex, user="ghost", company=world.company, public_key="pem").save()
    Device(id=uuid4().hex, user="ghost", company=world.company, public_key="pem", revoked=True).save()
    assert find_orphans(world.company) == {"device.user": [live.id]}


def test_a_repository_check_of_a_missing_user_is_an_orphan(world):
    from apiserver.bll.integrity import find_orphans

    check = RepositoryCheck(id=uuid4().hex, company=world.company, repository="r-any", user="ghost").save()
    assert find_orphans(world.company) == {"repository_check.user": [check.id]}


def test_deleting_a_user_removes_their_credentials_assignments_and_none_needed(world, monkeypatch):
    from apiserver.bll.integrity import find_orphans

    _patch(monkeypatch, "anonymous", lambda *args, **kwargs: True)
    credential(world, "mine", "member")
    credential(world, "second", "member", kind="token")
    credential(world, "theirs", "other")
    first = use(world)
    second = use(world, url=URL + "-2")
    third = use(world, user="other", url=URL + "-3", project=world.other_root)
    bll.assign(world.company, "member", first.id, "mine")
    bll.assign(world.company, "member", second.id, none_needed=True)
    bll.assign(world.company, "other", third.id, "theirs")
    delete_user(world, "member")
    assert not UserAccess.objects(company=world.company, user="member")
    assert not Assignment.objects(company=world.company, user="member")
    assert UserAccess.objects(id="theirs").first() is not None
    assert Assignment.objects(company=world.company, user="other", credential="theirs").count() == 1
    assert find_orphans(world.company) == {}


def test_deleting_a_user_keeps_a_company_credential_that_others_use(world):
    from apiserver.bll.integrity import find_orphans

    credential(world, "company", "admin", scope="company")
    repo = use(world)
    bll.assign(world.company, "member", repo.id, "company")
    bll.assign(world.company, "owner", repo.id, "company")
    delete_user(world, "member")
    # the deleted user's own assignment is gone; the credential and the other user's assignment are untouched and usable
    assert not Assignment.objects(company=world.company, user="member")
    assert UserAccess.objects(id="company").first() is not None
    assert Assignment.objects(company=world.company, user="owner", credential="company").count() == 1
    resolution = bll.resolve(bll.get(world.company, repo.id), None, "owner")
    assert resolution.state == bll.ASSIGNED and resolution.entry.id == "company"
    assert find_orphans(world.company) == {}


def test_a_company_credential_of_a_deleted_admin_passes_to_another_admin_of_the_company(world):
    from apiserver.bll.integrity import find_orphans

    AuthUser(id="admin2", name="Admin2", company=world.company, role="admin", created=datetime.utcnow()).save()
    credential(world, "company", "admin2", scope="company")
    repo = use(world)
    bll.assign(world.company, "member", repo.id, "company")
    delete_user(world, "admin2")
    entry = UserAccess.objects(id="company").first()
    assert entry is not None and entry.user == "admin"
    assert Assignment.objects(company=world.company, user="member", credential="company").count() == 1
    assert find_orphans(world.company) == {}


def test_a_company_credential_stays_when_no_other_admin_is_left_to_take_it(world, caplog):
    credential(world, "company", "admin", scope="company")
    repo = use(world)
    bll.assign(world.company, "member", repo.id, "company")
    with caplog.at_level("WARNING"):
        delete_user(world, "admin")
    # the credential and the assignment of another user stay usable; the reference to the missing admin is logged
    assert UserAccess.objects(id="company").first() is not None
    assert Assignment.objects(company=world.company, user="member", credential="company").count() == 1
    assert "no admin is left" in caplog.text


def test_the_stored_secrets_of_a_deleted_user_are_no_longer_in_the_database(world):
    credential(world, "mine", "member")
    credential(world, "theirs", "other")
    mine = UserAccess.objects(id="mine").first().secret
    theirs = UserAccess.objects(id="theirs").first().secret
    collection = UserAccess._get_collection()
    assert collection.count_documents({"secret": mine}) == 1
    delete_user(world, "member")
    assert collection.count_documents({"secret": mine}) == 0
    assert collection.count_documents({"user": "member"}) == 0
    assert collection.count_documents({"secret": theirs}) == 1  # another user's secret stays


def test_a_deactivated_user_is_deleted_with_their_records(world):
    credential(world, "mine", "member")
    repo = use(world)
    bll.assign(world.company, "member", repo.id, "mine")
    AuthUser.objects(id="member").update(idp_disabled=True)
    delete_user(world, "member")
    assert not UserProfile.objects(id="member").first()
    assert not UserAccess.objects(id="mine").first()
    assert not Assignment.objects(company=world.company, user="member")


def test_a_failed_cleanup_keeps_the_user_so_that_the_delete_can_be_repeated(world, monkeypatch):
    credential(world, "mine", "member")
    repo = use(world)
    bll.assign(world.company, "member", repo.id, "mine")

    def failing_delete(self, *args, **kwargs):
        raise RuntimeError("the database refused the delete")

    with monkeypatch.context() as patched:
        patched.setattr(UserAccess, "delete", failing_delete)
        with pytest.raises(Exception, match="refused"):
            delete_user(world, "member")
    assert UserProfile.objects(id="member").first() is not None
    delete_user(world, "member")
    assert not UserProfile.objects(id="member").first()
    assert not UserAccess.objects(id="mine").first()
    assert not Assignment.objects(company=world.company, user="member")


def test_deleting_an_unknown_user_changes_nothing(world):
    from apiserver.bll.user import UserBLL

    credential(world, "mine", "member")
    with pytest.raises(errors.bad_request.InvalidUserId):
        UserBLL.delete("nobody")
    assert UserAccess.objects(id="mine").first() is not None


def test_a_failing_credential_delete_leaves_no_orphan_and_keeps_the_credential(world, monkeypatch):
    from apiserver.bll import user_access
    from apiserver.bll.integrity import find_orphans

    credential(world, "mine", "member")
    repo = use(world)
    bll.assign(world.company, "member", repo.id, "mine")

    def failing_delete(self, *args, **kwargs):
        raise RuntimeError("the database refused the delete")

    monkeypatch.setattr(UserAccess, "delete", failing_delete)
    with pytest.raises(RuntimeError, match="refused"):
        user_access.delete(world.company, "member", "mine")
    # assignments go first, so the failure leaves an unused credential and never an assignment to a missing one
    assert UserAccess.objects(id="mine").first() is not None
    assert not Assignment.objects(company=world.company, credential="mine")
    assert find_orphans(world.company) == {}


def test_a_dangling_assignment_is_found(world):
    from apiserver.bll.integrity import find_orphans

    credential(world, "mine", "owner")
    repo = use(world)
    bll.assign(world.company, "owner", repo.id, "mine")
    Assignment.objects(repository=repo.id).update(credential="gone")
    assert find_orphans(world.company) == {"assignment.credential": [Assignment.objects(repository=repo.id).first().id]}
