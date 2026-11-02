"""The Datasets page: the datasets the GitHub App is installed on, and the details of one dataset (from its repository or
from the registry)."""

from apiserver.apierrors import errors
from apiserver.bll import dataset_access
from apiserver.bll import dataset_details as bll
from apiserver.bll import dataset_registry as registry_bll
from apiserver.idp.github import GitHubError
from apiserver.idp.github_check import build_from_config
from apiserver.service_repo import APICall, endpoint


@endpoint("datasets.list")
def list_datasets(call: APICall, _company: str, _request):
    # The repositories the GitHub App is installed on, that have a dataset.yaml at their root. The same for every company.
    app = build_from_config()
    if app is None:
        call.result.data = {"datasets": []}
        return
    try:
        call.result.data = {"datasets": dataset_access.list_datasets(app)}
    except GitHubError as exc:
        raise errors.bad_request.ValidationError(f"GitHub request failed ({exc})") from None


@endpoint("datasets.get_details")
def get_details(call: APICall, _company: str, _request):
    request = call.data or {}
    app = build_from_config()
    if app is None:
        raise errors.bad_request.ValidationError("GitHub is not configured on this server")
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
