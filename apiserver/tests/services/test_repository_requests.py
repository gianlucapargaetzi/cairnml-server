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


# --- the request classes ------------------------------------------------------------------------------------------------------------

ID_BASED = ["delete", "set_shared", "check_sharing", "link", "unlink", "assign", "unassign", "check", "refresh", "get_runs",
            "get_history", "get_dataset_versions"]
OTHERS = ["get_all", "add", "get_details", "git_token", "find", "clone", "resolve", "requirement"]
TYPED = ID_BASED + OTHERS


def model_of(action, service="repositories"):
    return ServiceRepo._get_endpoint(f"{service}.{action}", VERSION).request_data_model


@pytest.mark.parametrize("action", TYPED)
def test_unknown_field_is_refused(action):
    model = model_of(action)
    assert model is not None, f"repositories.{action} has no request class"
    with pytest.raises(TypeError, match="bogus"):
        model(bogus=1)


def test_an_unknown_field_is_a_validation_error_through_the_call():
    from apiserver.service_repo import APICall
    from apiserver.service_repo.validators import validate_data
    endpoint = ServiceRepo._get_endpoint("repositories.check", VERSION)
    api_call = APICall("repositories.check", data={"bogus": 1}, headers={}, remote_addr="127.0.0.1")
    with pytest.raises(errors.bad_request.ValidationError, match="bogus"):
        validate_data(api_call, endpoint)


def test_an_alias_logs_a_warning_with_the_action(world, caplog, seen):
    repo = bll.add(world.company, "owner", False, URL, "dataset")
    with caplog.at_level("WARNING", logger="apiserver.apimodels.repositories"):
        invoke("unassign", world, {"id": repo.id})
        invoke("assign", world, {"repository": URL})
        invoke("unassign", world, {"ref": repo.id})
    warnings = [r.getMessage() for r in caplog.records if r.name == "apiserver.apimodels.repositories"]
    assert len(warnings) == 2
    assert "repositories.unassign" in warnings[0] and "'id'" in warnings[0]
    assert "repositories.assign" in warnings[1] and "'repository'" in warnings[1]


def test_ref_wins_over_id_over_repository(world, seen):
    repo = bll.add(world.company, "owner", False, URL, "dataset")
    invoke("check", world, {"ref": repo.id, "id": "the-id", "repository": "the-repository"})
    invoke("check", world, {"id": repo.id, "repository": "the-repository"})
    assert seen == [repo.id, repo.id]


def test_no_identifier_is_refused_as_no_such_repository(world):
    with pytest.raises(errors.bad_request.ValidationError, match=r"no such repository: \)"):
        invoke("delete", world, {})


# the exact bodies the callers send today: the SDK (cairnml/datasets/credentials.py, clone.py) and the web app
# (repositories.service.ts, repo-access.service.ts, dataset-details.service.ts)
KNOWN_BODIES = [
    ("get_all", {}), ("get_all", {"project": "p1"}),
    ("add", {"url": URL, "role": "dataset"}),
    ("add", {"url": URL, "role": "code", "credential": "c", "none_needed": True, "shared": True, "project": "p1"}),
    ("git_token", {"repository": URL, "level": "read"}),
    ("git_token", {"ref": URL, "level": "read_write"}),
    ("assign", {"repository": URL, "role": "dataset", "credential": "c", "none_needed": True}),
    ("assign", {"id": "r1", "credential": "c"}),
    ("unassign", {"repository": URL}), ("check", {"repository": URL}), ("delete", {"repository": URL}),
    ("set_shared", {"id": "r1", "shared": True, "credential": "c", "none_needed": True}),
    ("check_sharing", {"id": "r1", "credential": "c", "none_needed": True}),
    ("link", {"id": "r1", "project": "p1"}), ("unlink", {"repository": URL, "project": "p1"}),
    ("get_history", {"id": "r1"}), ("get_runs", {"id": "r1"}), ("get_dataset_versions", {"id": "r1"}), ("refresh", {"id": "r1"}),
    ("get_details", {"repository": URL, "entry": "e", "commit": "abc"}), ("get_details", {"entry": "e"}),
    ("find", {"ref": URL}), ("clone", {"ref": URL, "commit": "abc"}), ("clone", {"ref": "cars"}),
    ("resolve", {"repositories": [URL], "project": "p1"}), ("resolve", {"repositories": [URL]}),
    ("requirement", {"tasks": ["t1"]}),
]


@pytest.mark.parametrize("action,body", KNOWN_BODIES, ids=lambda v: v if isinstance(v, str) else "")
def test_known_callers_bodies_are_accepted(action, body):
    model = model_of(action)
    assert model is not None, f"repositories.{action} has no request class"
    model(**body).validate()


USER_ACCESS = ["get_all", "add_app", "delete", "check"]


@pytest.mark.parametrize("action", USER_ACCESS)
def test_user_access_unknown_field_is_refused(action):
    model = ServiceRepo._get_endpoint(f"user_access.{action}", PartialVersion("2.38")).request_data_model
    assert model is not None, f"user_access.{action} has no request class"
    with pytest.raises(TypeError, match="bogus"):
        model(bogus=1)


@pytest.mark.parametrize("action,body", [
    ("get_all", {}), ("delete", {"id": "a1"}), ("check", {"id": "a1"}),
    ("add_app", {"label": "L", "app_id": "1", "api_url": "", "key": "k", "scope": "company"}),
    ("add_app", {"label": "L", "app_id": "1", "key": "k"}),
])
def test_user_access_known_bodies_are_accepted(action, body):
    model = ServiceRepo._get_endpoint(f"user_access.{action}", PartialVersion("2.38")).request_data_model
    assert model is not None
    model(**body).validate()
