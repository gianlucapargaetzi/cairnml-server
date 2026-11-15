"""Repository mutations: sharing, deletion, assignment of credentials, the check of a repository and the links to projects. The
records, the access decisions and the view live in repository_records, repository_access and repository_view; their names are
re-exported here so that existing callers keep working."""

import logging
import uuid
from datetime import datetime
from typing import Optional


from apiserver.apierrors import errors
from apiserver.bll import access, membership
from apiserver.database.model.repository import ROLES, Assignment, RegistryEntry, Repository, RepositoryCheck
from apiserver.database.model.share import Share
from apiserver.database.model.user_access import UserAccess
from apiserver.idp import repo_address

from apiserver.bll.repository_records import ASSIGNED, COMPANY, NONE_NEEDED, ANONYMOUS, UNKNOWN, UNREACHABLE  # noqa: F401
from apiserver.bll.repository_access import (  # noqa: F401
    ANONYMOUS_NO_TTL,
    ANONYMOUS_UNKNOWN_TTL,
    ANONYMOUS_YES_TTL,
    READ_PERMISSIONS,
    WRITE_PERMISSIONS,
)
from apiserver.bll.repository_records import (  # noqa: F401  (moved; re-exported for the existing callers)
    _create,
    _names,
    _reference_for,
    _root_and_owner,
    _with_role,
    admin_of,
    can_see,
    delete_blocked,
    find,
    find_reference,
    get,
    is_shared,
    level_of,
    linked_projects,
    parse,
    require_visible,
    visible,
)
from apiserver.bll.repository_access import (  # noqa: F401  (moved; re-exported for the existing callers)
    Resolution,
    _app_of,
    _app_token,
    _assigned,
    _check_reads,
    _company_reach,
    _company_token,
    _entry_problem,
    _fresh,
    _token_of,
    anonymous,
    credential_choices,
    resolve,
    token_for,
    token_for_job,
    token_for_person,
)
from apiserver.bll.repository_view import (  # noqa: F401  (moved; re-exported for the existing callers)
    _assignment_rows,
    _readable,
    _result_for,
    list_for,
    view,
)


log = logging.getLogger(__name__)


def record_use(
    company: str,
    address: str,
    role: str,
    user: str,
    project_id: Optional[str] = None,
    run_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[Repository]:
    """Records the repository a run uses. The record is made for the owner of the run's project when there is none (never for
    the user who ran it, unless they own the project), and the repository is linked to the project. A record that exists is
    only linked and given the role. None when the address names no hosted repository."""
    parsed = repo_address.parse(address or "")
    if parsed is None or not user:
        return None
    now = now or datetime.utcnow()
    root, owner = _root_and_owner(company, user, project_id)
    repo = Repository.objects(company=company, key=parsed.key).first()
    if repo is None:
        repo = _create(company, parsed, role, owner, now)
    else:
        _with_role(repo, role)
    repo.last_used = now
    repo.save()
    if root:
        _link(company, repo, root, owner, actor=user, inherited=True, run_id=run_id)
    return repo


def record_use_quietly(
    company: str, address: str, role: str, user: str, project_id: Optional[str] = None, run_id: Optional[str] = None
) -> Optional[Repository]:
    """``record_use`` for the places that save a task: a failure is logged and never refuses the save."""
    try:
        return record_use(company, address, role, user, project_id, run_id)
    except Exception:
        log.warning("the repository %s could not be recorded", address, exc_info=True)
        return None


def add(
    company: str,
    user: str,
    is_admin: bool,
    address: str,
    role: str,
    shared: bool = False,
    project_id: Optional[str] = None,
    credential: Optional[str] = None,
    none_needed: bool = False,
) -> Repository:
    """Lists a repository before any run has used it. A user's entry is non-shared; only the admin may add a shared one."""
    access.refuse_inactive(user)
    if role not in ROLES:
        raise errors.bad_request.ValidationError(f"the role is one of {', '.join(ROLES)}")
    if shared and not is_admin:
        raise errors.forbidden.NoWritePermission("only the admin adds a shared repository")
    parsed = parse(address)
    root = None
    if project_id:
        root = membership.root_project(company, project_id)
        if root is None:
            raise errors.bad_request.ValidationError("no such project")
        if not is_admin and not membership.is_member(company, root, user):
            raise errors.forbidden.NoWritePermission("you are not a member of this project")
    now = datetime.utcnow()
    repo = Repository.objects(company=company, key=parsed.key).first()
    if repo is None:
        owner = (admin_of(company) or user) if shared else user
        repo = _create(company, parsed, role, owner, now)
    else:
        if user in (repo.dismissed or []) and (repo.owner == user or is_admin):
            repo.update(pull__dismissed=user)  # listing it again brings back what the user deleted
            repo = get(company, repo.id)
        if not can_see(company, user, is_admin, repo):
            raise errors.bad_request.ValidationError(
                "this repository is already listed by someone else: ask for it to be shared"
            )
        _with_role(repo, role).save()
    if shared and not is_shared(repo):
        set_shared(company, user, is_admin, repo.id, True)
        repo = get(company, repo.id)
    if root:
        _link(company, repo, root, _root_and_owner(company, user, root)[1], actor=user, inherited=False, run_id=None)
    if credential or none_needed:
        assign(company, user, repo.id, credential, none_needed, is_admin)
    return repo


def _link(
    company: str, repo: Repository, root: str, root_owner: str, actor: str, inherited: bool, run_id: Optional[str]
):
    try:
        return access.record_grant(
            company,
            root_owner,
            "repository",
            repo.id,
            "projects",
            root,
            level="read",
            actor=actor,
            inherited=inherited,
            run_id=run_id,
        )
    except errors.bad_request.ValidationError:
        log.warning("the repository %s could not be linked to project %s", repo.key, root, exc_info=True)
        return None
    except errors.forbidden.NoWritePermission:
        return None  # a deactivated user links nothing


def link(company: str, user: str, is_admin: bool, repository_id: str, project_id: str) -> Share:
    """Lists a repository in a project, by hand. The user must see the repository and be a member of the project."""
    access.refuse_inactive(user)
    repo = require_visible(company, user, is_admin, repository_id)
    root = membership.root_project(company, project_id)
    if root is None:
        raise errors.bad_request.ValidationError("no such project")
    if not is_admin and not membership.is_member(company, root, user):
        raise errors.forbidden.NoWritePermission("you are not a member of this project")
    created = _link(
        company, repo, root, _root_and_owner(company, user, root)[1], actor=user, inherited=False, run_id=None
    )
    if created is None:
        raise errors.bad_request.ValidationError("the repository could not be listed in the project")
    return created


def unlink(company: str, user: str, is_admin: bool, repository_id: str, project_id: str) -> None:
    """Takes a repository out of a project's list: the project's owner and collaborators may, and so may the repository's owner.
    The repository and every assignment stay; a run that uses it again lists it again."""
    access.refuse_inactive(user)
    repo = require_visible(company, user, is_admin, repository_id)
    root = membership.root_project(company, project_id)
    share = access.find_share(company, "repository", repo.id, "projects", root or "")
    if share is None:
        raise errors.bad_request.ValidationError("the repository is not listed in this project")
    access.unshare(company, user, share.id)
    prune_assignments(company, repo.id)


def regrant_project_links(company: str, root: str, new_owner: str, actor: str) -> None:
    """The owner of a project changed: the repositories listed in it are now listed by its new owner (R20)."""
    for repository_id in access.resource_ids_shared_with(company, "repository", root):
        access.remove_grant(company, "repository", repository_id, "projects", root, actor=actor)
        access.record_grant(company, new_owner, "repository", repository_id, "projects", root, actor=actor)


def verify_for_sharing(
    company: str, user: str, is_admin: bool, repository_id: str, credential: Optional[str], none_needed: bool = False
) -> None:
    """Raises, with the reason, unless the company credential reads the repository (or the host clones it without a credential,
    for ``none_needed``): what a shared repository is read with must be confirmed before it is shared."""
    repo = require_visible(company, user, is_admin, repository_id)
    if credential and none_needed:
        raise errors.bad_request.ValidationError("choose a company credential or None needed, not both")
    if credential:
        entry = UserAccess.objects(company=company, id=credential, scope="company").first()
        if entry is None:
            raise errors.bad_request.ValidationError("a shared repository is read with a company credential")
        _check_reads(entry, repo)
    elif none_needed:
        answer = anonymous(repo, force=True)
        if answer is not True:
            raise errors.bad_request.ValidationError(
                f"{repo.host}/{repo.path} cannot be cloned without a credential, so None needed does not apply"
            )
    else:
        raise errors.bad_request.ValidationError(
            "choose the company credential that reads it, or None needed for a public repository"
        )


def set_shared(
    company: str,
    user: str,
    is_admin: bool,
    repository_id: str,
    shared: bool,
    credential: Optional[str] = None,
    none_needed: bool = False,
    confirm: bool = False,
) -> Repository:
    """Makes an entry shared, or non-shared again. The owner or the admin makes it shared: ownership moves to the admin, and
    the previous owner's assignment is dropped, as is any assignment of a credential that is not the company's (a shared
    entry is read with company credentials only). Only the admin makes it non-shared again. With ``confirm`` the company
    credential (or None needed) that will read it is verified first and becomes the admin's assignment."""
    access.refuse_inactive(user)
    repo = require_visible(company, user, is_admin, repository_id)
    if shared:
        if repo.owner != user and not is_admin:
            raise errors.forbidden.NoWritePermission("only the owner of a repository or the admin shares it")
        if is_shared(repo):
            return repo
        admin = admin_of(company)
        if admin is None:
            raise errors.bad_request.ValidationError("there is no admin to own a shared repository")
        if confirm:
            verify_for_sharing(company, user, is_admin, repository_id, credential, none_needed)
        previous = repo.owner
        repo.owner = admin
        repo.updated = datetime.utcnow()
        repo.save()
        for assignment in Assignment.objects(repository=repo.id):
            entry = UserAccess.objects(id=assignment.credential).first() if assignment.credential else None
            owned_before = assignment.user == previous and previous != admin
            if owned_before or (entry is not None and entry.scope != "company"):
                assignment.delete()
        access.record_grant(company, admin, "repository", repo.id, "public", actor=user)
        if confirm:
            reads = Assignment.objects(repository=repo.id, user=admin).first() or Assignment(
                id=uuid.uuid4().hex, company=company, repository=repo.id, user=admin, created=datetime.utcnow()
            )
            reads.credential, reads.none_needed = (credential or None), bool(none_needed)
            reads.save()
        return repo
    if not is_admin:
        raise errors.forbidden.NoWritePermission("only the admin makes a shared repository non-shared")
    access.remove_grant(company, "repository", repo.id, "public", actor=user)
    return repo


def delete(company: str, user: str, is_admin: bool, repository_id: str) -> None:
    """Deletes the repository for the user. A shared one is removed entirely (the admin only). A non-shared one goes away for the
    user who deletes it: their credential assignment is dropped and it is hidden from them; when nobody else holds an
    assignment on it, the record goes entirely. A repository a project lists is not deleted."""
    access.refuse_inactive(user)
    repo = require_visible(company, user, is_admin, repository_id)
    reason = delete_blocked(company, user, is_admin, repo)
    if reason:
        raise errors.bad_request.ValidationError(f"it cannot be deleted: {reason}")
    Assignment.objects(repository=repo.id, user=user).delete()
    RepositoryCheck.objects(repository=repo.id, user=user).delete()
    others = Assignment.objects(repository=repo.id, user__ne=user).count()
    if is_shared(repo) or not others:
        RegistryEntry.objects(company=company, registry=repo.id).delete()
        Assignment.objects(repository=repo.id).delete()
        RepositoryCheck.objects(repository=repo.id).delete()
        access.drop_resource_shares(company, "repository", repo.id, actor=user)
        repo.delete()
        return
    repo.update(add_to_set__dismissed=user)


def prune_assignments(company: str, repository_id: Optional[str] = None, user: Optional[str] = None) -> int:
    """Drops the assignments of users who no longer see their repository (they left a project that listed it, or it was taken out
    of the project) and clears what they deleted. A repository nobody holds, owns or lists any more is removed. Returns the
    number of assignments dropped."""
    query = {"company": company}
    if repository_id:
        query["repository"] = repository_id
    if user:
        query["user"] = user
    dropped = 0
    for assignment in list(Assignment.objects(**query)):
        repo = Repository.objects(company=company, id=assignment.repository).first()
        if repo is None:
            assignment.delete()
            continue
        if not membership.is_active(assignment.user):
            continue
        if level_of(company, assignment.user, False, repo) is None:
            assignment.delete()
            dropped += 1
    for stored in list(RepositoryCheck.objects(**query)):  # a user without an assignment has a stored check too
        # as for the assignments above, a disabled user's data is kept
        repo = Repository.objects(company=company, id=stored.repository).first()
        if repo is None or (membership.is_active(stored.user) and level_of(company, stored.user, False, repo) is None):
            stored.delete()
    return dropped


def assign(
    company: str,
    user: str,
    repository_id: str,
    credential: Optional[str] = None,
    none_needed: bool = False,
    is_admin: bool = False,
) -> Optional[Assignment]:
    """Assigns the user's own credential (or the company's), or "None needed", to a repository they see; with neither, the
    assignment is cleared. The credential must read the repository. "None needed" only applies when the host clones it
    without a credential. Only the user's own assignment is changed, whoever they are."""
    access.refuse_inactive(user)
    repo = require_visible(company, user, is_admin, repository_id)
    if credential and none_needed:
        raise errors.bad_request.ValidationError("choose a credential or None needed, not both")
    if not credential and not none_needed:
        unassign(company, user, repo.id, is_admin)
        return None
    if credential:
        entry = next((e for e in credential_choices(company, user, repo) if e.id == credential), None)
        if entry is None:
            raise errors.bad_request.ValidationError(
                "that credential is not one you may assign here: "
                + (
                    "a shared repository is read with a company credential"
                    if is_shared(repo)
                    else "choose one of your own credentials or the company's"
                )
            )
        _check_reads(entry, repo)
    else:
        answer = anonymous(repo, force=True)
        if answer is None:
            raise errors.bad_request.ValidationError(
                f"{repo.host} could not be asked whether {repo.path} can be cloned without a credential: try again in a moment"
            )
        if answer is not True:
            raise errors.bad_request.ValidationError(
                f"{repo.host}/{repo.path} cannot be cloned without a credential, so None needed does not apply"
            )
    mine = Assignment.objects(repository=repo.id, user=user).first()
    if mine is None:
        mine = Assignment(
            id=uuid.uuid4().hex, company=company, repository=repo.id, user=user, created=datetime.utcnow()
        )
    mine.credential, mine.none_needed = (credential or None), bool(none_needed)
    mine.save()
    return mine


def unassign(company: str, user: str, repository_id: str, is_admin: bool = False) -> None:
    """Clears the user's own assignment. Nobody else's is touched."""
    access.refuse_inactive(user)
    repo = require_visible(company, user, is_admin, repository_id)
    Assignment.objects(repository=repo.id, user=user).delete()


def check(company: str, user: str, is_admin: bool, repository_id: str) -> Repository:
    """Looks at the repository again: asks the host whether it is cloned without a credential (the kept answer is renewed, and being
    the same for everybody it is kept on the record) and tries how the user reads it, with their own assignment. What it finds is
    the user's own result, kept for them (``RepositoryCheck``): how another user reads the repository is not how this one does, so
    it is shown to nobody else and the shared record is not written."""
    repo = require_visible(company, user, is_admin, repository_id)
    anonymous(repo, force=True)
    resolution = resolve(repo, None, user)
    state, message = "ok", ""
    mine = Assignment.objects(repository=repo.id, user=user).first()
    if mine is not None and mine.credential and resolution.state == ASSIGNED:
        mine.update(last_used=datetime.utcnow())
        try:
            _check_reads(resolution.entry, repo)
        except errors.bad_request.ValidationError as refused:
            state, message = "no_access", f"The assigned credential cannot read it: {refused.msg}."
    elif resolution.state == UNREACHABLE:
        state, message = "no_access", resolution.message
    RepositoryCheck.objects(repository=repo.id, user=user).modify(
        upsert=True,
        new=True,
        set_on_insert__id=uuid.uuid4().hex,
        set__company=company,
        set__state=state,
        set__message=message,
        set__checked=datetime.utcnow(),
    )
    return repo
