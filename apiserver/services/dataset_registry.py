"""The public dataset registries: the datasets and adapters the server lists. A dataset of our own is not registered."""

import logging

from apiserver.apierrors import errors
from apiserver.apimodels.base import UpdateResponse
from apiserver.apimodels.dataset_registry import RegistryRequest
from apiserver.bll import dataset_registry as bll
from apiserver.database.model.auth import Role
from apiserver.idp.audit import client_address, record as audit
from apiserver.idp.github import GitHubError
from apiserver.idp.github_check import build_from_config
from apiserver.idp.secrets import SecretMissing
from apiserver.service_repo import APICall, endpoint

log = logging.getLogger(__name__)
_MANAGE_ROLES = (Role.admin, Role.root)


@endpoint("dataset_registry.get_all")
def get_all(call: APICall, _company: str, _request):
    # The registries are shared by the server, so the list is the same for every company. A registry that cannot be read
    # (for example a private one the GitHub App is not installed on) is left out, and the others are still listed.
    try:
        app = build_from_config()
    except SecretMissing:
        # No key on this server: nothing can be read. The GitHub App status says why (github.status).
        log.warning("the GitHub App key is missing; the dataset registries are not read", exc_info=True)
        app = None
    registries = bll.configured_registries()
    if app is None or not registries:
        call.result.data = {"datasets": []}
        return
    datasets, seen = [], set()
    for registry in registries:
        try:
            entries = bll.list_entries(app=app, registry=registry)
        except GitHubError:
            log.warning("the dataset registry %s could not be read; it is left out of the list", registry, exc_info=True)
            continue
        for entry in entries:
            if entry["name"] not in seen:
                seen.add(entry["name"])
                datasets.append({**entry, "registry": registry})
    call.result.data = {"datasets": datasets}


@endpoint("dataset_registry.get_registries")
def get_registries(call: APICall, _company: str, _request):
    """The registries the server reads, for everyone to see. Changed with dataset_registry.add and remove."""
    call.result.data = {"registries": [
        {**row, "added": row["added"].isoformat()} for row in bll.list_registries()
    ]}


def _assert_can_manage(call: APICall) -> None:
    if call.identity.role not in _MANAGE_ROLES:
        raise errors.forbidden.RoleNotAllowed("only an admin may change the dataset registries")


@endpoint("dataset_registry.add", response_data_model=UpdateResponse, request_data_model=RegistryRequest)
def add(call: APICall, _company: str, request: RegistryRequest):
    _assert_can_manage(call)
    repository = bll.normalize_repository(request.repository)
    if repository is None:
        raise errors.bad_request.ValidationError(
            "a registry must be a GitHub repository: https://github.com/owner/name.git or git@github.com:owner/name.git")
    added = bll.add_registry(repository, added_by=call.identity.user)
    audit(call.identity.user, "dataset_registry.add", ip=client_address(call), repository=repository, added=added)
    call.result.data_model = UpdateResponse(updated=int(added))


@endpoint("dataset_registry.remove", response_data_model=UpdateResponse, request_data_model=RegistryRequest)
def remove(call: APICall, _company: str, request: RegistryRequest):
    _assert_can_manage(call)
    repository = bll.normalize_repository(request.repository)
    if repository is None:
        raise errors.bad_request.ValidationError(
            "a registry must be a GitHub repository: https://github.com/owner/name.git or git@github.com:owner/name.git")
    removed = bll.remove_registry(repository)
    audit(call.identity.user, "dataset_registry.remove", ip=client_address(call), repository=repository, removed=removed)
    call.result.data_model = UpdateResponse(updated=int(removed))
