"""The Datasets page: the datasets the company can see (their owner, visibility, the credential that reads them), the
details of one dataset (from its repository or from the registry), and adding, sharing and removing them. See
bll/datasets for who may do what."""

from apiserver.apierrors import errors
from apiserver.bll import access
from apiserver.bll import dataset_details as bll
from apiserver.bll import datasets as access_bll
from apiserver.database.model.auth import Role
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.device import DeviceKind
from apiserver.idp import github_check
from apiserver.idp.audit import client_address, record as audit
from apiserver.idp.github import GitHubError, RepoNotAccessible
from apiserver.idp.github_check import build_from_config, public_repo, repo_of
from apiserver.service_repo import APICall, endpoint


def _is_admin(call: APICall) -> bool:
    return call.identity.role in (Role.admin, Role.root)


@endpoint("datasets.visible")
def visible(call: APICall, company: str, _request):
    """The datasets the signed-in user may see, each with its owner, visibility and state."""
    call.result.data = {"datasets": access_bll.list_visible(company, call.identity.user, _is_admin(call))}


@endpoint("datasets.add")
def add(call: APICall, company: str, _request):
    data = call.data or {}
    if data.get("kind") == "registry":
        call.result.data = {"dataset": access_bll.add_registry(
            company, call.identity.user, data.get("url", ""), visibility=data.get("visibility") or "user",
            project=data.get("project") or None)}
        return
    call.result.data = {"dataset": access_bll.add_repo(
        company, call.identity.user, data.get("url", ""), visibility=data.get("visibility") or "user",
        credential=data.get("credential") or None)}


@endpoint("datasets.set_credential")
def set_credential(call: APICall, company: str, _request):
    data = call.data or {}
    call.result.data = {"dataset": access_bll.set_credential(
        company, call.identity.user, data.get("repository", ""), data.get("credential") or None, _is_admin(call))}
    audit(call.identity.user, "dataset.credential_set", ip=client_address(call), company=company,
          repo=data.get("repository", ""), credential=data.get("credential") or "")


@endpoint("datasets.set_visibility")
def set_visibility(call: APICall, company: str, _request):
    data = call.data or {}
    call.result.data = {"dataset": access_bll.set_visibility(
        company, call.identity.user, data.get("id", ""), data.get("visibility", ""), data.get("projects") or [],
        _is_admin(call))}


@endpoint("datasets.allocate")
def allocate(call: APICall, company: str, _request):
    data = call.data or {}
    call.result.data = {"dataset": access_bll.allocate(
        company, call.identity.user, data.get("id", ""), data.get("project", ""), _is_admin(call))}


@endpoint("datasets.unallocate")
def unallocate(call: APICall, company: str, _request):
    data = call.data or {}
    call.result.data = {"dataset": access_bll.unallocate(
        company, call.identity.user, data.get("id", ""), data.get("project", ""), _is_admin(call))}


@endpoint("datasets.delete")
def delete(call: APICall, company: str, _request):
    data = call.data or {}
    access_bll.delete(company, call.identity.user, data.get("id", ""), _is_admin(call))
    call.result.data = {"deleted": True}


def _visible_of(call: APICall, company: str, rows: list) -> list:
    """The kept records the caller may see. Refuses when none is, as a missing dataset, so that the existence of a dataset
    the caller may not see is not revealed. No records: nothing to check, and nothing is returned."""
    if not rows:
        return []
    shown = [row for row in rows if access_bll.can_see(company, call.identity.user, _is_admin(call), row["id"])]
    if not shown:
        raise errors.bad_request.ValidationError("no such dataset")
    return shown


@endpoint("datasets.get_details")
def get_details(call: APICall, company: str, _request):
    request = call.data or {}
    if request.get("registry_name"):
        # a registry's entry is shown from the entry stored when its registry was added or refreshed: no GitHub call
        name = request["registry_name"]
        entries = access_bll.registry_entries_named(company, name)
        if not entries:
            raise errors.bad_request.ValidationError(f"no dataset {name} in the registries of this server")
        shown = _visible_of(call, company, [access_bll.view(entry) for entry in entries])
        entry = next(entry for entry in entries if entry.id == shown[0]["id"])
        call.result.data = bll.details(None, repository=None, registry_entry=entry, commit=None)
        return
    repo = bll.repository_of(request.get("repository"))
    dataset = access_bll.kept_for_repository(company, repo)
    _visible_of(call, company, [access_bll.view(dataset)] if dataset is not None else [])
    if dataset is not None and dataset.kind == "repo":
        # a dataset of ours is read with the credential it holds, and no other
        app = access_bll.reader_for_dataset(company, dataset, repo)
    else:
        app = github_check.reader()  # the App when there is one; public repositories need none
    try:
        call.result.data = bll.details(app, repository=repo, registry_entry=None, commit=request.get("commit"))
    except GitHubError as exc:
        raise errors.bad_request.ValidationError(f"GitHub request failed ({exc})") from None


# A person's token for a dataset repository. read downloads and needs the use action; read_write changes the dataset and
# needs change_content (bll/access). A read token reads the repository; a write token can push to it, and comes from the
# GitHub App's installation for that one repository.
GIT_TOKEN_ACTIONS = {"read": "use", "read_write": "change_content"}
READ_PERMISSIONS = {"contents": "read"}
# a push to a dataset repository also writes its workflow file (.github/workflows), which needs the workflows permission
WRITE_PERMISSIONS = {"contents": "write", "workflows": "write"}


def _refuse_token(call: APICall, company: str, user: str, repo: str, level: str, reason: str):
    audit(user, "git_token.refused", ip=client_address(call), company=company, repo=repo, level=level, reason=reason)
    raise errors.bad_request.ValidationError(reason)


@endpoint("datasets.git_token")
def git_token(call: APICall, company: str, _request):
    """A token for one dataset repository, for a person's machine, at the level the person holds on the dataset: read
    gives a read-only token, read_write a token that may write to that repository and no other. A person with no share
    is refused as not found. A job or an agent is refused: the job scope and the agent allowlist refuse them for every
    endpoint outside their own, and this endpoint refuses them by token kind as well."""
    kind = getattr(call.auth, "token_kind", None)
    if kind in ("job", DeviceKind.agent):
        raise errors.forbidden.NoWritePermission(f"a {kind} cannot get a dataset token: a person's machine does")
    data = call.data or {}
    action = GIT_TOKEN_ACTIONS.get(data.get("level"))
    if action is None:
        raise errors.bad_request.ValidationError("level must be read or read_write")
    repo = repo_of(data.get("repository") or "")
    user = call.identity.user
    try:
        level, resource_type = access_bll.held_on_repository(company, user, _is_admin(call), repo) if repo else (None, "dataset")
    except access_bll.NoGitHubRead as refused:
        _refuse_token(call, company, user, repo, None, str(refused))
    if level is None:
        raise errors.bad_request.ValidationError("no such dataset")
    if not access.allows(level, action, resource_type):
        raise errors.forbidden.NoWritePermission(f"a {level} level does not allow {action} on this {resource_type}")

    write = action == "change_content"
    if not write:
        try:
            public = public_repo(repo)
        except GitHubError:
            raise errors.bad_request.ValidationError("GitHub could not be reached") from None
        if public:
            # cloned without a token, as a public repository always is
            audit(user, "git_token.public", ip=client_address(call), company=company, repo=repo, level=level)
            return {"token": None, "expires_at": None, "username": None}
    dataset = Dataset.objects(company=company, kind="repo", ref__iexact=repo).first()
    if dataset is not None:
        # a dataset is read with the credential it was added with, and no other (see bll/datasets.token_for_person)
        try:
            answer = access_bll.token_for_person(
                company, user, dataset, repo, write, WRITE_PERMISSIONS if write else READ_PERMISSIONS)
        except errors.bad_request.ValidationError as refused:
            _refuse_token(call, company, user, repo, level, str(refused))
        audit(user, "git_token.issued", ip=client_address(call), company=company, repo=repo, level=level)
        return answer
    # a repository the company's App finds with no record of its own: read with the company's App, as its list shows
    app = build_from_config()
    if app is None:
        _refuse_token(call, company, user, repo, level, "GitHub is not configured")
    try:
        token = app.installation_token(repo, WRITE_PERMISSIONS if write else READ_PERMISSIONS)
    except RepoNotAccessible:
        reason = f"the GitHub App is not installed on {repo}" + (" with write access to its contents" if write else "")
        _refuse_token(call, company, user, repo, level, reason)
    except GitHubError as exc:
        _refuse_token(call, company, user, repo, level, f"GitHub did not issue a token for {repo} ({exc})")
    audit(user, "git_token.issued", ip=client_address(call), company=company, repo=repo, level=level)
    return {"token": token.token, "expires_at": token.expires_at.isoformat(), "username": "x-access-token"}
