"""Repositories: code, datasets and registries of datasets, listed once, read with one credential mechanism. See
bll/repositories. The only service for them."""

from apiserver.apierrors import errors
from apiserver.apimodels import repositories as models
from apiserver.bll import repositories as bll
from apiserver.bll import (
    dataset_versions,
    repository_details,
    repository_history,
    repository_registry,
    repository_runs,
    membership,
)
from apiserver.database.model.auth import Role
from apiserver.database.model.repository import Repository
from apiserver.database.model.task.task import Task
from apiserver.idp.audit import client_address, record as audit
from apiserver.service_repo import APICall, endpoint

MAX_REPOSITORIES = 50


def _is_admin(call: APICall) -> bool:
    return call.identity.role in (Role.admin, Role.root)


def _view(call: APICall, company: str, repo: Repository, entries: bool = True) -> dict:
    return bll.view(company, call.identity.user, _is_admin(call), repo, entries)


def _repository_of(call: APICall, company: str, request: models.RepositoryRefRequest, action: str) -> Repository:
    """The repository a request names (``ref``: its id, name or address). One the caller does not see is refused as missing."""
    reference = request.reference()
    repo = bll.find_reference(company, reference)
    if repo is None or not bll.can_see(company, call.identity.user, _is_admin(call), repo):
        raise errors.bad_request.ValidationError(f"no such repository: {reference}")
    return repo


@endpoint("repositories.get_all")
def get_all(call: APICall, company: str, request: models.GetAllRequest):
    call.result.data = {
        "repositories": bll.list_for(company, call.identity.user, _is_admin(call), request.project or None)
    }


@endpoint("repositories.add")
def add(call: APICall, company: str, request: models.AddRequest):
    repo = bll.add(
        company,
        call.identity.user,
        _is_admin(call),
        request.url,
        request.role,
        shared=bool(request.shared),
        project_id=request.project or None,
        credential=request.credential or None,
        none_needed=bool(request.none_needed),
    )
    audit(
        call.identity.user,
        "repository.added",
        ip=client_address(call),
        company=company,
        repo=repo.key,
        role=request.role,
        shared=bool(request.shared),
    )
    if "registry" in repo.roles and repo.read_at is None:
        try:  # its entries are there the moment it is listed; one that cannot be read yet says why on its record
            repository_registry.read_entries(
                company, repo, call.identity.user, membership.root_project(company, request.project)
            )
        except errors.bad_request.ValidationError:
            pass
        repo = bll.get(company, repo.id)
    call.result.data = {"repository": _view(call, company, repo)}


@endpoint("repositories.delete")
def delete(call: APICall, company: str, request: models.DeleteRequest):
    repo = _repository_of(call, company, request, "repositories.delete")
    bll.delete(company, call.identity.user, _is_admin(call), repo.id)
    audit(call.identity.user, "repository.deleted", ip=client_address(call), company=company, repo=repo.key)
    call.result.data = {"deleted": True}


@endpoint("repositories.set_shared")
def set_shared(call: APICall, company: str, request: models.SetSharedRequest):
    repo = _repository_of(call, company, request, "repositories.set_shared")
    shared = bool(request.shared)
    repo = bll.set_shared(
        company,
        call.identity.user,
        _is_admin(call),
        repo.id,
        shared,
        credential=request.credential or None,
        none_needed=bool(request.none_needed),
        confirm=shared,
    )
    audit(
        call.identity.user,
        "repository.shared" if shared else "repository.unshared",
        ip=client_address(call),
        company=company,
        repo=repo.key,
    )
    call.result.data = {"repository": _view(call, company, repo)}


@endpoint("repositories.check_sharing")
def check_sharing(call: APICall, company: str, request: models.CheckSharingRequest):
    """Whether a company credential (or None needed) reads the repository, so that it may be shared: ``ok`` and the reason."""
    repo = _repository_of(call, company, request, "repositories.check_sharing")
    try:
        bll.verify_for_sharing(
            company, call.identity.user, _is_admin(call), repo.id, request.credential or None, bool(request.none_needed)
        )
    except errors.bad_request.ValidationError as refused:
        call.result.data = {"ok": False, "message": refused.msg}
        return
    call.result.data = {"ok": True, "message": ""}


@endpoint("repositories.link")
def link(call: APICall, company: str, request: models.LinkRequest):
    repo = _repository_of(call, company, request, "repositories.link")
    bll.link(company, call.identity.user, _is_admin(call), repo.id, request.project)
    call.result.data = {"repository": _view(call, company, repo)}


@endpoint("repositories.unlink")
def unlink(call: APICall, company: str, request: models.UnlinkRequest):
    repo = _repository_of(call, company, request, "repositories.unlink")
    bll.unlink(company, call.identity.user, _is_admin(call), repo.id, request.project)
    call.result.data = {"repository": _view(call, company, repo)}


@endpoint("repositories.assign")
def assign(call: APICall, company: str, request: models.AssignRequest):
    """The caller's own assignment on a repository, named by id or by address. An address no one has listed yet is listed first,
    as the caller's own non-shared entry with the role asked for (a dataset by default)."""
    reference = request.reference()
    repo = bll.find_reference(company, reference)
    if repo is not None and not bll.can_see(company, call.identity.user, _is_admin(call), repo):
        repo = None  # listed by someone else: adding it says so
    if repo is None and bll.repo_address.parse(reference) is not None:
        repo = bll.add(company, call.identity.user, _is_admin(call), reference, request.role or "dataset")
    if repo is None:
        raise errors.bad_request.ValidationError(f"no such repository: {reference}")
    bll.assign(
        company, call.identity.user, repo.id, request.credential or None, bool(request.none_needed), _is_admin(call)
    )
    audit(
        call.identity.user,
        "repository.credential_set",
        ip=client_address(call),
        company=company,
        repo=repo.key,
        credential=request.credential or "",
        none_needed=bool(request.none_needed),
    )
    call.result.data = {"repository": _view(call, company, repo)}


@endpoint("repositories.unassign")
def unassign(call: APICall, company: str, request: models.UnassignRequest):
    repo = _repository_of(call, company, request, "repositories.unassign")
    bll.unassign(company, call.identity.user, repo.id, _is_admin(call))
    audit(call.identity.user, "repository.credential_cleared", ip=client_address(call), company=company, repo=repo.key)
    call.result.data = {"repository": _view(call, company, repo)}


@endpoint("repositories.check")
def check(call: APICall, company: str, request: models.CheckRequest):
    repo = _repository_of(call, company, request, "repositories.check")
    repo = bll.check(company, call.identity.user, _is_admin(call), repo.id)
    row = _view(call, company, repo)  # the caller's own result: what the audit records too
    audit(
        call.identity.user,
        "repository.checked",
        ip=client_address(call),
        company=company,
        repo=repo.key,
        state=row["state"],
    )
    call.result.data = {"repository": row}


@endpoint("repositories.refresh")
def refresh(call: APICall, company: str, request: models.RefreshRequest):
    repo = _repository_of(call, company, request, "repositories.refresh")
    counts = repository_registry.refresh(company, call.identity.user, _is_admin(call), repo.id)
    call.result.data = {"counts": counts, "repository": _view(call, company, bll.get(company, repo.id))}


@endpoint("repositories.get_details")
def get_details(call: APICall, company: str, request: models.GetDetailsRequest):
    call.result.data = repository_details.details(
        company,
        call.identity.user,
        _is_admin(call),
        repository=request.reference(),
        entry=request.entry,
        commit=request.commit,
    )


def _job_user(call: APICall, company: str):
    """(the user a job acts for, its project) when the call is a job's, else (None, None): the one who queued its task, else the
    task's own user."""
    job_task = getattr(call.auth, "job_task", None) if call.auth is not None else None
    if not job_task:
        return None, None
    task = Task.objects(company=company, id=job_task).first()
    if task is None:
        return None, None
    enqueuer = task.execution.enqueued_by if task.execution else None
    return enqueuer or task.user, task.project


def _may_read(call: APICall, company: str, repo: Repository) -> bool:
    """Whether the caller may use the repository. A person: what they hold on it. A job: what the one who queued its task holds,
    or what its project lists (a run lists the repositories it uses)."""
    job_user, project = _job_user(call, company)
    if job_user is None:
        return bll.can_see(company, call.identity.user, _is_admin(call), repo)
    root = membership.root_project(company, project) if project else None
    return bll.can_see(company, job_user, False, repo) or (
        root is not None and bll.access.find_share(company, "repository", repo.id, "projects", root) is not None
    )


@endpoint("repositories.get_runs")
def get_runs(call: APICall, company: str, request: models.GetRunsRequest):
    repo = _repository_of(call, company, request, "repositories.get_runs")
    call.result.data = {"runs": repository_runs.runs_of(company, call.identity.user, _is_admin(call), repo)}


@endpoint("repositories.get_history")
def get_history(call: APICall, company: str, request: models.GetHistoryRequest):
    """The condensed history of a repository: the newest commit, every commit a task ran on with its tasks, the first commit, and
    a gap with the number of commits between them. See bll/repository_history."""
    repo = _repository_of(call, company, request, "repositories.get_history")
    call.result.data = repository_history.history(company, call.identity.user, _is_admin(call), repo)


@endpoint("repositories.get_dataset_versions")
def get_dataset_versions(call: APICall, company: str, request: models.GetDatasetVersionsRequest):
    """The samples and splits of a dataset at the commits that matter (see bll/dataset_versions), oldest first."""
    repo = _repository_of(call, company, request, "repositories.get_dataset_versions")
    call.result.data = {"versions": dataset_versions.versions(company, call.identity.user, _is_admin(call), repo)}


@endpoint("repositories.find")
def find(call: APICall, company: str, request: models.FindRequest):
    """The repository a reference names: its id, its name, or the address of its repository. One the caller may not see is
    refused as missing, the same as one that does not exist. A job may call this for its own task only."""
    reference = request.ref
    repo = bll.find_reference(company, reference)
    if repo is None or not _may_read(call, company, repo):
        raise errors.bad_request.ValidationError(f"no such dataset: {reference}")
    call.result.data = {"repository": _view(call, company, repo)}


@endpoint("repositories.clone")
def clone(call: APICall, company: str, request: models.CloneRequest):
    """What a client needs to clone a dataset repository: its address, the commit to clone (the head when none is named) and a
    token made with the credential that reads it for the caller (none when it is cloned without one)."""
    repo = bll.find_reference(company, request.ref)
    if repo is None or not _may_read(call, company, repo):
        raise errors.bad_request.ValidationError(f"no such dataset: {request.ref}")
    job_user, project = _job_user(call, company)
    if job_user is not None:
        answer, _resolution = bll.token_for_job(company, repo, project, job_user)
    else:
        answer = bll.token_for_person(company, call.identity.user, _is_admin(call), repo)
    call.result.data = {
        "repository": f"https://{repo.host}/{repo.path}.git",
        "dataset": _view(call, company, repo, entries=False),
        "commit": request.commit or None,
        "token": answer["token"],
        "expires_at": answer["expires_at"],
        "username": answer["username"],
    }


@endpoint("repositories.git_token")
def git_token(call: APICall, company: str, request: models.GitTokenRequest):
    """A token for one repository, for a person's machine: read for anyone who sees it, read_write for its owner and the admin
    (a push). A job or an agent is refused: its own endpoint, devices.git_token, serves them. A public repository needs no
    token for a read: ``token`` is null."""
    kind = getattr(call.auth, "token_kind", None)
    if kind in ("job", "agent"):
        raise errors.forbidden.NoWritePermission(
            f"a {kind} cannot get a repository token here: a person's machine does"
        )
    level = request.level
    if level not in ("read", "read_write"):
        raise errors.bad_request.ValidationError("level must be read or read_write")
    reference = request.reference()
    parsed = bll.repo_address.parse(reference)
    repo = bll.find(company, reference) if parsed is not None else None
    if repo is None or not bll.can_see(company, call.identity.user, _is_admin(call), repo):
        if parsed is not None and level == "read" and bll.is_private(parsed.url) is False:
            # a public repository nobody listed yet needs no token and no record: a run lists it when it records what it used
            audit(
                call.identity.user,
                "git_token.public",
                ip=client_address(call),
                company=company,
                repo=parsed.key,
                level=level,
            )
            call.result.data = {"token": None, "expires_at": None, "username": None}
            return
        raise errors.bad_request.ValidationError(NO_SUCH_REPOSITORY_HINT)
    answer = bll.token_for_person(company, call.identity.user, _is_admin(call), repo, write=level == "read_write")
    audit(
        call.identity.user,
        "git_token.issued" if answer["token"] else "git_token.public",
        ip=client_address(call),
        company=company,
        repo=repo.key,
        level=level,
    )
    call.result.data = {"token": answer["token"], "expires_at": answer["expires_at"], "username": answer["username"]}


NO_SUCH_REPOSITORY_HINT = (
    "no such repository: make sure it is listed and readable by assigning a valid credential to it "
    "(web app: Repositories and Datasets, Add)"
)


@endpoint("repositories.resolve")
def resolve(call: APICall, company: str, request: models.ResolveRequest):
    """How the caller reads each repository in a project: for the badges."""
    repositories = request.references()
    if len(repositories) > MAX_REPOSITORIES:
        raise errors.bad_request.ValidationError(f"give a list of at most {MAX_REPOSITORIES} repositories")
    call.result.data = {
        "repositories": repository_runs.resolve_many(
            company, repositories, call.identity.user, request.project or None, _is_admin(call)
        )
    }


@endpoint("repositories.requirement")
def requirement(call: APICall, company: str, request: models.RequirementRequest):
    """What the tasks' repositories need before the caller dispatches them: the warning of the web app."""
    tasks = list(request.tasks)
    if len(tasks) > 500:
        raise errors.bad_request.ValidationError("give a list of at most 500 tasks")
    call.result.data = {
        "repositories": repository_runs.requirement(company, call.identity.user, tasks, _is_admin(call))
    }
