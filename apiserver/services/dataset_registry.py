"""The dataset registries: a GitHub repository listing datasets, added by any user. Their entries are listed on the Datasets
page. Registries added before datasets were kept are still listed; new ones are kept as datasets (bll/datasets)."""

import logging

from apiserver.apierrors import errors
from apiserver.apimodels.base import UpdateResponse
from apiserver.apimodels.dataset_registry import RegistryRequest
from apiserver.bll import dataset_registry as bll
from apiserver.bll import datasets as access_bll
from apiserver.database.model.auth import Role
from apiserver.idp import github_check
from apiserver.idp.audit import client_address, record as audit
from apiserver.idp.github import GitHubError
from apiserver.service_repo import APICall, endpoint

log = logging.getLogger(__name__)
_REMOVE_ROLES = (Role.admin, Role.root)


def _is_admin(call: APICall) -> bool:
    return call.identity.role in (Role.admin, Role.root)


@endpoint("dataset_registry.get_all")
def get_all(call: APICall, company: str, _request):
    """The dataset entries the user may see: the entries of registries added as datasets, then those of the older
    registries. Each entry says which registry lists it."""
    entries = []
    for row in access_bll.list_visible(company, call.identity.user, _is_admin(call)):
        if row["kind"] != "registry_entry":
            continue
        entries.append({"name": row["name"], "source": row["source"], "link": row["link"],
                        "registry": row["ref"].rsplit("/", 1)[0]})
    seen = {entry["name"] for entry in entries}
    registries = bll.configured_registries()
    if registries:
        # the older registries are read with the App, or anonymously when they are public
        app = github_check.reader()
        for registry in registries:
            try:
                listed = bll.list_entries(app=app, registry=registry)
            except GitHubError:
                log.warning("the dataset registry %s could not be read; it is left out of the list", registry,
                            exc_info=True)
                continue
            for entry in listed:
                if entry["name"] not in seen:
                    seen.add(entry["name"])
                    entries.append({**entry, "registry": registry})
    call.result.data = {"datasets": entries}


@endpoint("dataset_registry.get_registries")
def get_registries(call: APICall, company: str, _request):
    """The registries the user may see, for everyone to see. Added with dataset_registry.add, removed with the
    dataset's own delete (datasets.delete), or dataset_registry.remove for the older ones."""
    rows = [row for row in access_bll.list_visible(company, call.identity.user, _is_admin(call))
            if row["kind"] == "registry"]
    # the id is the dataset's, so its owner can remove it (datasets.delete); an older registry has none and only an admin
    # removes it (dataset_registry.remove)
    registries = [{"id": None if row["id"].startswith("registry:") else row["id"], "repository": row["ref"],
                   "url": f"https://github.com/{row['ref']}", "added": row["added"], "added_by": row["owner"]}
                  for row in rows]
    seen = {row["repository"].lower() for row in registries}
    for legacy in bll.list_registries():
        if legacy["repository"].lower() not in seen:
            registries.append({**legacy, "added": legacy["added"].isoformat()})
    call.result.data = {"registries": registries}


@endpoint("dataset_registry.add", response_data_model=UpdateResponse, request_data_model=RegistryRequest)
def add(call: APICall, company: str, request: RegistryRequest):
    """Any user may add a registry: it is kept as a dataset owned by them, public to the company. Its entries are added
    with it. A registry that cannot be read is refused with the reason."""
    repository = bll.normalize_repository(request.repository)
    if repository is None:
        raise errors.bad_request.ValidationError(
            "a registry must be a GitHub repository: https://github.com/owner/name.git or git@github.com:owner/name.git")
    access_bll.add_registry(company, call.identity.user, f"https://github.com/{repository}", visibility="Public")
    audit(call.identity.user, "dataset_registry.add", ip=client_address(call), repository=repository)
    call.result.data_model = UpdateResponse(updated=1)


@endpoint("dataset_registry.remove", response_data_model=UpdateResponse, request_data_model=RegistryRequest)
def remove(call: APICall, _company: str, request: RegistryRequest):
    """Removes one of the older registries. Only an admin, as it was before. Newer ones are removed with datasets.delete."""
    if call.identity.role not in _REMOVE_ROLES:
        raise errors.forbidden.RoleNotAllowed("only an admin may remove an older dataset registry")
    repository = bll.normalize_repository(request.repository)
    if repository is None:
        raise errors.bad_request.ValidationError(
            "a registry must be a GitHub repository: https://github.com/owner/name.git or git@github.com:owner/name.git")
    removed = bll.remove_registry(repository)
    audit(call.identity.user, "dataset_registry.remove", ip=client_address(call), repository=repository, removed=removed)
    call.result.data_model = UpdateResponse(updated=int(removed))
