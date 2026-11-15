"""The request classes of the repositories service: the old identifier names still work, one name (ref) wins over them, an
unknown field is refused. See docs/compatibility/repository-identifier.md."""
import pytest

from apiserver.apierrors import errors
from apiserver.apierrors.apierror import APIError
from apiserver.bll import repositories as bll
from apiserver.service_repo import ServiceRepo
from apiserver.services import repositories as svc
from apiserver.utilities.partial_version import PartialVersion
from apiserver.tests.services.test_repositories_service import URL, call, world  # noqa: F401  (the fixture)

VERSION = PartialVersion("2.40")

# the actions that name a repository, with the extra fields each needs to get past its own checks
ID_ACTIONS = {
    "delete": {}, "set_shared": {}, "check_sharing": {}, "link": {"project": "P"}, "unlink": {"project": "P"},
    "assign": {}, "unassign": {}, "check": {}, "refresh": {}, "get_runs": {}, "get_history": {},
    "get_dataset_versions": {}, "get_details": {}, "git_token": {"level": "read"},
}


def invoke(action, world, data, user="owner"):
    """Run the handler of ``repositories.<action>`` as the real call does: the request class of the endpoint built from the
    body (none while an endpoint has no class)."""
    model = ServiceRepo._get_endpoint(f"repositories.{action}", VERSION).request_data_model
    request = model(**data) if model else None
    c = call(user, data)
    return getattr(svc, action)(c, world.company, request)


@pytest.fixture
def seen(world, monkeypatch):
    """The references the handlers look a repository up with."""
    looked_up = []
    for module, name in ((bll, "find_reference"), (bll, "find")):
        original = getattr(module, name)
        monkeypatch.setattr(module, name, lambda company, reference, _o=original, **kw: (looked_up.append(reference), _o(company, reference, **kw))[1])
    from apiserver.bll import repository_details
    monkeypatch.setattr(repository_details, "details", lambda company, user, is_admin, repository=None, **kw: looked_up.append(repository) or {})
    return looked_up


def try_invoke(action, world, data):
    """Run it; the action may stop past the lookup (a host, a registry, a project rule): only the lookup is under test."""
    try:
        invoke(action, world, data)
    except errors.bad_request.ValidationError as refused:
        assert not str(refused.msg).startswith("no such repository: ") or action == "git_token", refused.msg
    except APIError:
        pass


@pytest.mark.parametrize("action", sorted(ID_ACTIONS))
@pytest.mark.parametrize("alias", ["id", "repository"])
def test_old_names_still_work(world, seen, action, alias):
    if alias == "id" and action in ("get_details", "git_token"):
        pytest.skip("these two never took id")
    repo = bll.add(world.company, "owner", False, URL, "dataset", project_id=world.root)
    value = repo.id if alias == "id" else URL
    extra = {k: (world.root if k == "project" else v) for k, v in ID_ACTIONS[action].items()}
    try_invoke(action, world, {alias: value, **extra})
    assert seen and seen[0] == value


def test_resolve_still_takes_repositories(world, monkeypatch):
    got = []
    monkeypatch.setattr(svc.repository_runs, "resolve_many", lambda company, items, *a: got.append(items) or [])
    c = call("owner", {"repositories": [URL]})
    model = ServiceRepo._get_endpoint("repositories.resolve", VERSION).request_data_model
    svc.resolve(c, world.company, model(**c.data) if model else None)
    assert got == [[URL]]
