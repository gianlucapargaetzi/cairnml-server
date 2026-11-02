"""The Datasets page: the details of one dataset, from its repository or from the registry."""

from apiserver.apierrors import errors
from apiserver.bll import dataset_details as bll
from apiserver.config_repo import config
from apiserver.idp.github import GitHubError
from apiserver.idp.github_check import build_from_config
from apiserver.service_repo import APICall, endpoint


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
            registry=config.get("apiserver.dataset_registry.repository", ""),
            registry_name=request.get("registry_name"),
            commit=request.get("commit"),
        )
    except GitHubError as exc:
        raise errors.bad_request.ValidationError(f"GitHub request failed ({exc})") from None
