"""The Datasets page: the datasets the company can see (their owner, visibility, the credential that reads them), the
details of one dataset (from its repository or from the registry), and adding, sharing and removing them. See
bll/datasets for who may do what."""

from apiserver.apierrors import errors
from apiserver.bll import dataset_cache
from apiserver.bll import dataset_details as bll
from apiserver.bll import datasets as access_bll
from apiserver.bll import dataset_registry as registry_bll
from apiserver.database.model.auth import Role
from apiserver.idp import github_check
from apiserver.idp.github import GitHubError
from apiserver.service_repo import APICall, endpoint


def _is_admin(call: APICall) -> bool:
    return call.identity.role in (Role.admin, Role.root)


@endpoint("datasets.list")
def list_datasets(call: APICall, _company: str, _request):
    # The cached list (bll/dataset_cache): it never calls GitHub here. refreshed_at is the last full rebuild.
    call.result.data = dataset_cache.cached_list()


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


@endpoint("datasets.get_details")
def get_details(call: APICall, _company: str, _request):
    request = call.data or {}
    app = github_check.reader()  # the App when there is one; public datasets and registries need none
    try:
        call.result.data = bll.details(
            app,
            repository=request.get("repository"),
            registries=registry_bll.configured_registries(),
            registry_name=request.get("registry_name"),
            commit=request.get("commit"),
        )
    except GitHubError as exc:
        raise errors.bad_request.ValidationError(f"GitHub request failed ({exc})") from None
