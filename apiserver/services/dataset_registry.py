"""The public dataset registries: the datasets and adapters the server lists. A dataset of our own is not registered."""

import logging

from apiserver.bll import dataset_registry as bll
from apiserver.idp.github import GitHubError
from apiserver.idp.github_check import build_from_config
from apiserver.service_repo import APICall, endpoint

log = logging.getLogger(__name__)


@endpoint("dataset_registry.get_all")
def get_all(call: APICall, _company: str, _request):
    # The registries are shared by the server, so the list is the same for every company. A registry that cannot be read
    # (for example a private one the GitHub App is not installed on) is left out, and the others are still listed.
    app = build_from_config()
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
