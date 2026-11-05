"""The Datasets page: the datasets the company can see (their owner, visibility, the credential that reads them), the
details of one dataset (from its repository or from the registry), and adding, sharing and removing them. See
bll/datasets for who may do what."""

from apiserver.apierrors import errors
from apiserver.bll import dataset_details as bll
from apiserver.bll import datasets as access_bll
from apiserver.database.model.auth import Role
from apiserver.idp import github_check
from apiserver.idp.github import GitHubError
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
    add_one = access_bll.add_registry if data.get("kind") == "registry" else access_bll.add_repo
    call.result.data = {"dataset": add_one(
        company, call.identity.user, data.get("url", ""), visibility=data.get("visibility") or "user",
        project=data.get("project") or None)}


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
    registries = []  # the registries that list the named entry, which the caller may see
    if request.get("registry_name"):
        entries = access_bll.registry_entries_named(company, request["registry_name"])
        shown = _visible_of(call, company, [access_bll.view(entry) for entry in entries])
        registries = list(dict.fromkeys(row["ref"].rsplit("/", 1)[0] for row in shown))
    else:
        dataset = access_bll.kept_for_repository(company, bll.repository_of(request.get("repository")))
        _visible_of(call, company, [access_bll.view(dataset)] if dataset is not None else [])
    app = github_check.reader()  # the App when there is one; public datasets and registries need none
    try:
        call.result.data = bll.details(
            app,
            repository=request.get("repository"),
            registries=registries,
            registry_name=request.get("registry_name"),
            commit=request.get("commit"),
        )
    except GitHubError as exc:
        raise errors.bad_request.ValidationError(f"GitHub request failed ({exc})") from None
