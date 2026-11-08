from types import SimpleNamespace

import pytest

from apiserver.database.model.auth import Role
from apiserver.database.model.device import DeviceKind
from apiserver.services import devices
from apiserver.tests.db import ensure_db

ensure_db()


class FakeQuery:
    def __init__(self, obj):
        self.obj = obj

    def first(self):
        return self.obj


class FakeCollection:
    """Stands in for Device.objects / User.objects: returns the one given object for any lookup."""

    def __init__(self, obj):
        self.obj = obj

    def __call__(self, **_):
        return FakeQuery(self.obj)


@pytest.fixture
def owner():
    return SimpleNamespace(id="u1", company="c1", role=Role.admin, name="Ada", idp_disabled=False)


def exchange_as(monkeypatch, owner, kind):
    """Runs devices.exchange for a device of ``kind`` owned by ``owner``; returns the role given to the token."""
    device = SimpleNamespace(id="dev_1", user="u1", company="c1", kind=kind, revoked=False,
                             public_key="key", last_used=None, save=lambda: None)
    issued = {}

    monkeypatch.setattr(devices, "unverified_issuer", lambda _assertion: "dev_1")
    monkeypatch.setattr(devices.Device, "objects", FakeCollection(device))
    monkeypatch.setattr(devices.User, "objects", FakeCollection(owner))
    monkeypatch.setattr(devices, "load_public_key", lambda _key: None)
    monkeypatch.setattr(devices, "verify_assertion",
                        lambda *_a, **_k: {"jti": "j1", "exp": 2e10})
    monkeypatch.setattr(devices, "claim_jti", lambda *_a, **_k: True)
    monkeypatch.setattr(devices, "public_origin", SimpleNamespace(api_url=lambda: "https://api"))
    monkeypatch.setattr(devices, "audit", lambda *_a, **_k: None)
    monkeypatch.setattr(devices, "client_address", lambda _call: "10.0.0.1")
    monkeypatch.setattr(devices, "config", SimpleNamespace(get=lambda _key: 3600))

    def fake_issue(user_id, company_id, device_id, kind, role, user_name="", company_name=""):
        issued.update(user=user_id, kind=kind, role=role)
        return "token"

    monkeypatch.setattr(devices, "issue_access_token", fake_issue)

    call = SimpleNamespace(authorization="Bearer x")
    request = SimpleNamespace(assertion="a.b.c")
    response = devices.exchange(call, None, request)

    assert response["access_token"] == "token"
    return issued


def test_an_agent_token_carries_the_user_role_even_when_the_owner_is_admin(monkeypatch, owner):
    issued = exchange_as(monkeypatch, owner, DeviceKind.agent)

    assert issued["kind"] == DeviceKind.agent
    assert issued["role"] == Role.user


def test_a_client_token_keeps_the_owner_role(monkeypatch, owner):
    issued = exchange_as(monkeypatch, owner, DeviceKind.client)

    assert issued["kind"] == DeviceKind.client
    assert issued["role"] == Role.admin
