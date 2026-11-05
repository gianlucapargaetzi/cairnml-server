"""The dataset registries: a GitHub repository listing datasets, added by any user. Their entries are listed on the Datasets
page. A registry is kept as a dataset of its own, and its entries are kept as datasets too (bll/datasets)."""

from apiserver.apierrors import errors
from apiserver.apimodels.base import UpdateResponse
from apiserver.apimodels.dataset_registry import RegistryRequest
from apiserver.bll import dataset_registry as bll
from apiserver.bll import datasets as access_bll
from apiserver.database.model.auth import Role
from apiserver.idp.audit import client_address, record as audit
from apiserver.service_repo import APICall, endpoint


def _is_admin(call: APICall) -> bool:
    return call.identity.role in (Role.admin, Role.root)


@endpoint("dataset_registry.get_all")
def get_all(call: APICall, company: str, _request):
    """The dataset entries the user may see, from the registries kept as datasets. Each entry says which registry lists
    it."""
    entries = []
    for row in access_bll.list_visible(company, call.identity.user, _is_admin(call)):
        if row["kind"] != "registry_entry":
            continue
        entries.append({"name": row["name"], "source": row["source"], "link": row["link"],
                        "registry": row["ref"].rsplit("/", 1)[0]})
    call.result.data = {"datasets": entries}


@endpoint("dataset_registry.get_registries")
def get_registries(call: APICall, company: str, _request):
    """The registries the user may see, for everyone to see. Added with dataset_registry.add, removed with the
    dataset's own delete (datasets.delete)."""
    rows = [row for row in access_bll.list_visible(company, call.identity.user, _is_admin(call))
            if row["kind"] == "registry"]
    registries = [{"id": row["id"], "repository": row["ref"], "url": f"https://github.com/{row['ref']}",
                   "added": row["added"], "added_by": row["owner"]}
                  for row in rows]
    call.result.data = {"registries": registries}


@endpoint("dataset_registry.add", response_data_model=UpdateResponse, request_data_model=RegistryRequest)
def add(call: APICall, company: str, request: RegistryRequest):
    """Any user may add a registry: it is kept as a dataset owned by them, with the visibility the request gives (user
    when none is given, as for a dataset: Datasets spec 4a). Its entries are added with it, with the same visibility.
    A registry that cannot be read is refused with the reason."""
    repository = bll.normalize_repository(request.repository)
    if repository is None:
        raise errors.bad_request.ValidationError(
            "a registry must be a GitHub repository: https://github.com/owner/name.git or git@github.com:owner/name.git")
    access_bll.add_registry(company, call.identity.user, f"https://github.com/{repository}",
                            visibility=request.visibility or "user")
    audit(call.identity.user, "dataset_registry.add", ip=client_address(call), repository=repository)
    call.result.data_model = UpdateResponse(updated=1)
