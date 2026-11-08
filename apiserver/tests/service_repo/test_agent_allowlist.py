"""An agent token may call only the endpoints the cairnml-agent calls (the allowlist)."""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from apiserver.apierrors import errors
from apiserver.database.model.auth import Role
from apiserver.service_repo import APICall, ServiceRepo
from apiserver.service_repo.auth import Identity, Token
from apiserver.service_repo.auth import auth as auth_module
from apiserver.service_repo.validators import AGENT_ALLOWED_ENDPOINTS, validate_auth, validate_role
from apiserver.utilities.partial_version import PartialVersion

TESTS_DIR = Path(__file__).resolve().parents[1]
AGENT_CALLS = TESTS_DIR / "e2e_support" / "agent_calls.txt"
SCHEMA_DIR = TESTS_DIR.parents[0] / "schema" / "services"

SENSITIVE = [
    "git_credentials.set",
    "git_credentials.delete",
    "git_credentials.get_all",
    "datasets.delete",
    "datasets.set_visibility",
    "datasets.allocate",
    "datasets.unallocate",
    "datasets.add",
    "dataset_registry.add",
    "devices.get_all",
    "devices.revoke",
    "devices.enroll",
    "audit.get_all",
]


def _user_access_names():
    names = re.findall(r"^(\w+) \{", (SCHEMA_DIR / "user_access.conf").read_text(), flags=re.MULTILINE)
    return [f"user_access.{name}" for name in names if name != "_default"]


def _agent_names():
    lines = AGENT_CALLS.read_text().splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def _endpoint(name):
    return SimpleNamespace(name=name, authorize=False, allows=lambda role: True)


def _call(token_kind):
    return SimpleNamespace(
        identity=SimpleNamespace(role=Role.admin),
        auth=SimpleNamespace(token_kind=token_kind),
    )


def test_the_agent_calls_file_is_sorted_and_free_of_duplicates():
    names = _agent_names()
    assert names, "agent_calls.txt lists no endpoints"
    assert names == sorted(names)
    assert len(names) == len(set(names))


@pytest.mark.parametrize("name", _agent_names())
def test_every_endpoint_the_agent_calls_is_allowed(name):
    assert name in AGENT_ALLOWED_ENDPOINTS


@pytest.mark.parametrize("name", _agent_names())
def test_an_agent_token_may_call_every_endpoint_the_agent_calls(name):
    validate_role(_endpoint(name), _call("agent"))


def test_no_storage_endpoint_is_allowed_for_agents():
    assert not [name for name in AGENT_ALLOWED_ENDPOINTS if name.startswith("storage.")]


@pytest.mark.parametrize("name", SENSITIVE + _user_access_names())
def test_a_sensitive_endpoint_is_not_in_the_allowlist(name):
    assert name not in AGENT_ALLOWED_ENDPOINTS


@pytest.mark.parametrize("name", SENSITIVE + _user_access_names())
def test_a_sensitive_endpoint_is_refused_for_an_agent_token(name):
    with pytest.raises(errors.forbidden.NoWritePermission):
        validate_role(_endpoint(name), _call("agent"))


@pytest.mark.parametrize("name", ["tasks.delete", "projects.create", "models.delete", "users.get_all"])
def test_an_endpoint_the_agent_does_not_call_is_refused_for_an_agent_token(name):
    with pytest.raises(errors.forbidden.NoWritePermission):
        validate_role(_endpoint(name), _call("agent"))


def test_the_allowlist_is_not_applied_to_user_tokens():
    validate_role(_endpoint("tasks.delete"), _call("web"))
    validate_role(_endpoint("projects.create"), _call("web"))


# --- the real call handling, for endpoints that do not authorise their callers ---------------------------

V28 = PartialVersion("2.8")


def _bearer(kind, **claims):
    identity = Identity(user="owner-1", company="company-1", role=Role.admin, user_name="owner")
    return "Bearer " + Token.create_encoded_token(identity, expiration_sec=600, kind=kind, **claims)


@pytest.fixture
def signed_in_tokens(monkeypatch):
    """Tokens are real and signed; the device and owner checks against the database are not needed here."""
    monkeypatch.setattr(auth_module, "check_idp_token", lambda token: None)
    if ServiceRepo._get_endpoint("devices.enroll", V28) is None:
        ServiceRepo.load()


def test_an_agent_bearer_token_is_refused_on_devices_enroll_by_the_real_call_handling(signed_in_tokens):
    call = APICall(
        "devices.enroll",
        endpoint_version=V28,
        headers={"Authorization": _bearer("agent", device="device-1")},
        data={"kind": "agent", "name": "host", "public_key": "not-used"},
    )
    ServiceRepo.handle_call(call)
    assert call.result.code == 403, call.result.msg
    assert "devices.enroll" in call.result.msg


def test_an_agent_bearer_token_is_refused_on_a_sensitive_endpoint_in_validate_auth(signed_in_tokens):
    call = APICall("devices.enroll", endpoint_version=V28, headers={"Authorization": _bearer("agent", device="device-1")})
    with pytest.raises(errors.forbidden.NoWritePermission):
        validate_auth(_endpoint_no_auth("devices.enroll"), call)


def test_a_person_enrolling_a_machine_is_not_refused(signed_in_tokens):
    call = APICall("devices.enroll", endpoint_version=V28, headers={"Authorization": _bearer("web", user="owner-1")})
    validate_auth(_endpoint_no_auth("devices.enroll"), call)


def test_an_anonymous_call_to_devices_enroll_is_not_refused(signed_in_tokens):
    validate_auth(_endpoint_no_auth("devices.enroll"), APICall("devices.enroll", endpoint_version=V28))


def test_an_unparsable_token_on_a_sensitive_endpoint_changes_nothing(signed_in_tokens):
    call = APICall("devices.enroll", endpoint_version=V28, headers={"Authorization": "Bearer not-a-token"})
    validate_auth(_endpoint_no_auth("devices.enroll"), call)


def _endpoint_no_auth(name):
    return SimpleNamespace(name=name, authorize=False, allows=lambda role: True)
