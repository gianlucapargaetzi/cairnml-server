"""Registration of datasets in the dataset registry."""

from apiserver.apierrors import errors
from apiserver.apimodels.dataset_registry import RegisterRequest
from apiserver.bll import dataset_registry as bll
from apiserver.config_repo import config
from apiserver.idp.github import GitHubError
from apiserver.idp.github_check import build_from_config
from apiserver.service_repo import APICall, endpoint


@endpoint("dataset_registry.get_all")
def get_all(call: APICall, _company: str, _request):
    # The registry is one repository for the whole server, so the list is the same for every company.
    app = build_from_config()
    registry = config.get("apiserver.dataset_registry.repository", "")
    if app is None or not registry:
        call.result.data = {"datasets": []}
        return
    try:
        entries = bll.list_entries(app=app, registry=registry)
    except GitHubError as exc:
        raise errors.bad_request.ValidationError(f"the dataset registry could not be read ({exc})") from None
    call.result.data = {"datasets": entries}


@endpoint("dataset_registry.register", request_data_model=RegisterRequest)
def register(call: APICall, _company: str, request: RegisterRequest):
    app = build_from_config()
    if app is None:
        raise errors.bad_request.ValidationError("GitHub is not configured on this server")
    call.result.data = bll.register(
        app=app,
        repository=request.repository,
        name=request.name,
        registry=config.get("apiserver.dataset_registry.repository", ""),
        allowed_owners=config.get("apiserver.dataset_registry.allowed_owners", []),
    )
