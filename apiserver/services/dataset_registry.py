"""Registration of datasets in the dataset registry."""

from apiserver.apierrors import errors
from apiserver.apimodels.dataset_registry import RegisterRequest
from apiserver.bll import dataset_registry as bll
from apiserver.config_repo import config
from apiserver.idp.github_check import build_from_config
from apiserver.service_repo import APICall, endpoint


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
