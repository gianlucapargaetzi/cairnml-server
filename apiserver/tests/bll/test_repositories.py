"""Repositories: one record, one credential mechanism. Who sees what, who may assign what, and how one repository is read for a
run in a project by a user (apiserver/bll/repositories)."""
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.bll import access, repositories as bll
from apiserver.database.model.auth import User
from apiserver.database.model.project import Project, ProjectMember, ProjectRole
from apiserver.database.model.repository import Assignment, RegistryEntry, Repository
from apiserver.database.model.share import Share
from apiserver.database.model.user_access import UserAccess
from apiserver.idp import git_credentials as store
from apiserver.tests.db import ensure_db

ensure_db()
URL = "https://github.com/acme/proj"


@pytest.fixture
def world(monkeypatch):
    """A company with an admin, a project owned by ``owner`` with ``member`` in it, and two outsiders. GitHub is replaced: the host
    says a repository is private, a credential reads every repository, and the company has no App."""
    company = f"c-rep-{uuid4().hex[:8]}"
    now = datetime.utcnow()
    for user, role in (("admin", "admin"), ("owner", "user"), ("member", "user"), ("other", "user"), ("stranger", "user")):
        User(id=user, name=user.title(), company=company, role=role, created=now).save()
    root = f"p-{company}"
    Project(id=root, company=company, user="owner", name="P", basename="P", created=now, last_update=now,
            members=[ProjectMember(user="member", role=ProjectRole.read, added=now, added_by="owner")]).save()
    other_root = f"q-{company}"
    Project(id=other_root, company=company, user="other", name="Q", basename="Q", created=now, last_update=now).save()
    state = SimpleNamespace(company=company, root=root, other_root=other_root, private=True, reads=True, apps=[],
                            reach=(None, "no company GitHub App is set up"), probes=[])

    def fake_private(address):
        state.probes.append(address)
        return state.private

    def fake_check_reads(entry, repo):
        if not state.reads:
            raise errors.bad_request.ValidationError(f"the GitHub App {entry.label} is not installed on {repo.path}")

    monkeypatch.setattr(store, "key_material", lambda: "test-key-material")
    monkeypatch.setattr(bll, "is_private", fake_private)
    monkeypatch.setattr(bll, "_check_reads", fake_check_reads)
    monkeypatch.setattr(bll, "_company_reach", lambda repo: state.reach)
    monkeypatch.setattr(bll.user_access, "company_apps", lambda company=None: state.apps)
    yield state
    for model in (Repository, RegistryEntry, Assignment, Share, UserAccess, Project, User):
        model.objects(company=company).delete()


def credential(world, entry_id, user, scope="user", kind="app"):
    UserAccess(id=entry_id, company=world.company, user=user, kind=kind, app_id="1", label=f"cred {entry_id}", scope=scope,
               host="github.com", path="", secret=store.encrypt("pem", store.key_material()), created=datetime.utcnow()).save()
    return entry_id


def use(world, user="owner", url=URL, role="code", project=None):
    return bll.record_use(world.company, url, role, user, project or world.root)


def rows(world, user, is_admin=False, project=None):
    return bll.list_for(world.company, user, is_admin, project)


# --- records ------------------------------------------------------------------------------------------------------------------

def test_every_spelling_of_an_address_is_one_repository(world):
    spellings = [URL, URL + ".git", "git@github.com:Acme/Proj.git", "ssh://git@github.com/acme/proj", "GIT://GitHub.com/ACME/proj/"]
    ids = {use(world, url=spelling).id for spelling in spellings}
    assert len(ids) == 1 and Repository.objects(company=world.company).count() == 1


def test_a_run_makes_the_record_for_the_owner_of_its_project_and_lists_it_there(world):
    repo = use(world, user="member")                  # a member who does not own the project ran it
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
    assert not Assignment.objects(repository=repo.id, user="owner")            # the former owner's assignment is dropped
    assert Assignment.objects(repository=repo.id, user="member").first().none_needed   # none needed is kept


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
    assert Repository.objects(company=world.company).count() == 0 and Assignment.objects(company=world.company).count() == 0
    assert RegistryEntry.objects(company=world.company).count() == 0 and Share.objects(company=world.company, resource_type="repository").count() == 0


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
    monkeypatch.setattr(bll, "_token_of", lambda entry, r, permissions=None: {"token": f"tok-{entry.id}", "expires_at": None, "username": "x"})
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
        (u["project"], u["as"]) for u in by_user["owner"]["used_in"]] == [(world.root, "owner")]
    assert row["relation"] == "inherited" and rows(world, "owner")[0]["relation"] == "own"
    assert "secret" not in str(row)


def test_the_list_says_what_the_user_may_do(world):
    repo = use(world)
    owner_row, member_row = rows(world, "owner")[0], rows(world, "member")[0]
    assert owner_row["can_share"] and not member_row["can_share"] and repo.id == owner_row["id"]
    assert not owner_row["can_delete"] and "used in P" in owner_row["delete_blocked"].replace("It is ", "")   # a project lists it
    bll.unlink(world.company, "owner", False, repo.id, world.root)
    assert rows(world, "owner")[0]["can_delete"] and rows(world, "owner")[0]["delete_blocked"] is None


def test_a_shared_row_is_owned_by_the_company_and_deleted_by_the_admin_only(world):
    bll.add(world.company, "admin", True, URL, "dataset", shared=True)
    row = rows(world, "stranger")[0]
    assert row["shared"] and row["owner_name"] == "Company" and not row["can_delete"] and not row["can_share"]
    assert "admin" in row["delete_blocked"]
    assert rows(world, "admin", True)[0]["can_delete"] and rows(world, "admin", True)[0]["can_share"]


def test_checking_stores_what_it_found(world):
    credential(world, "mine", "member")
    repo = use(world)
    bll.assign(world.company, "member", repo.id, "mine")
    assert bll.check(world.company, "member", False, repo.id).state == "ok"
    world.reads = False
    checked = bll.check(world.company, "member", False, repo.id)
    assert checked.state == "no_access" and "not installed" in checked.state_message and checked.last_checked


def test_the_owner_of_a_project_changing_relinks_its_repositories(world):
    repo = use(world)
    bll.regrant_project_links(world.company, world.root, "member", actor="admin")
    link = access.find_share(world.company, "repository", repo.id, "projects", world.root)
    assert link is not None and link.granted_by == "member"


def test_a_project_whose_owner_has_no_assignment_is_a_row_of_the_owner_without_a_credential(world):
    use(world)
    row = rows(world, "member")[0]
    assert [(a["user_name"], a["credential_name"], a["readable"], a["can_edit"]) for a in row["assignments"]] == [("Owner", "", False, False)]
    assert [(u["project"], u["as"], u["member"]) for u in row["assignments"][0]["used_in"]] == [(world.root, "owner", True)]
    assert rows(world, "owner")[0]["assignments"][0]["can_edit"]            # the owner may assign from that row


def test_the_assignment_that_reads_for_a_job_is_marked_used(world, monkeypatch):
    monkeypatch.setattr(bll, "_token_of", lambda entry, r, permissions=None: {"token": "tok", "expires_at": None, "username": "x"})
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
    assert not bll.is_shared(bll.get(world.company, repo.id))              # nothing changed when it was refused
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
    assert [(a["credential_name"], a["readable"], a["can_edit"], a["company"]) for a in reading] == [("cred company", True, False, True)]


# --- deleting: for the user who deletes it, never while a project depends on it ---------------------------------------------------

def test_a_repository_a_project_lists_cannot_be_deleted_and_the_reason_is_given(world):
    repo = use(world)
    with pytest.raises(errors.bad_request.ValidationError, match="project depends on it"):
        bll.delete(world.company, "owner", False, repo.id)
    row = rows(world, "owner")[0]
    assert row["can_delete"] is False and "P" in row["delete_blocked"]
    assert rows(world, "stranger") == []                                  # a user who cannot see it learns nothing of the projects
    with pytest.raises(errors.bad_request.ValidationError, match="project depends on it"):
        bll.delete(world.company, "admin", True, repo.id)                 # not even the admin: the project depends on it


def test_deleting_what_others_still_hold_removes_it_for_the_user_only(world):
    credential(world, "mine", "owner")
    repo = bll.add(world.company, "owner", False, URL, "dataset")
    bll.assign(world.company, "owner", repo.id, "mine")
    Assignment(id="a-member", company=world.company, repository=repo.id, user="member", credential="x").save()   # someone else holds one
    bll.delete(world.company, "owner", False, repo.id)
    assert Repository.objects(company=world.company).count() == 1                 # the record stays
    assert not Assignment.objects(repository=repo.id, user="owner") and Assignment.objects(repository=repo.id, user="member")
    assert rows(world, "owner") == []                                             # gone for the one who deleted it
    again = bll.add(world.company, "owner", False, URL, "dataset")                  # listing it again brings it back
    assert again.id == repo.id and [r["name"] for r in rows(world, "owner")] == ["proj"]


def test_a_project_that_lists_a_repository_shows_it_again_to_a_user_who_deleted_it(world):
    repo = bll.add(world.company, "owner", False, URL, "dataset")
    Assignment(id="a-member", company=world.company, repository=repo.id, user="member", credential="x").save()
    bll.delete(world.company, "owner", False, repo.id)
    assert rows(world, "owner") == []
    use(world)                                                                    # a run in the owner's project lists it
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
    Project.objects(id=world.root).update_one(pull__members__user="member")      # the member is removed from the project
    assert bll.prune_assignments(world.company, user="member") == 1
    assert not Assignment.objects(repository=repo.id, user="member") and rows(world, "member") == []
    assert [r["name"] for r in rows(world, "owner")] == ["proj"]                   # the others keep it


def test_a_user_who_still_uses_the_repository_in_a_project_of_their_own_keeps_the_credential(world):
    credential(world, "other-cred", "other")
    repo = use(world, user="other", project=world.other_root)                      # the record is the owner's; Q lists it too
    use(world)
    bll.assign(world.company, "other", repo.id, "other-cred")
    bll.unlink(world.company, "owner", False, repo.id, world.root)                 # taken out of P: Q still lists it
    assert Assignment.objects(repository=repo.id, user="other") and [r["name"] for r in rows(world, "other")] == ["proj"]


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
    repo = use(world)                                                             # P: its owner assigns a credential below
    use(world, user="other", project=world.other_root)                            # Q: nobody assigns one, and it is private
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
