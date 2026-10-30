from types import SimpleNamespace

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.devices import ExchangeRequest, GitTokenRequest
from apiserver.service_repo.auth import Token
from apiserver.services import devices


@pytest.fixture
def audited(monkeypatch):
    """Collects the audit records the devices service writes, instead of saving them."""
    recorded = []
    monkeypatch.setattr(devices, "audit", lambda actor, action, **detail: recorded.append((actor, action, detail)))
    return recorded


def call_from(address):
    headers = {"X-Real-IP": address, "Authorization": "Bearer x"}
    return SimpleNamespace(
        authorization="Bearer x",
        get_header=lambda header, default=None: headers.get(header, default),
        HEADER_REAL_IP="X-Real-IP",
        HEADER_FORWARDED_FOR="X-Forwarded-For",
        real_ip=address,
    )


def test_a_refused_git_token_request_is_audited_with_the_proxy_address(monkeypatch, audited):
    monkeypatch.setattr(Token, "decode", lambda _token: {"kind": "web", "device": "dev_1"})

    with pytest.raises(errors.unauthorized.InvalidToken):
        devices.git_token(call_from("10.0.0.5"), None, GitTokenRequest(task="t1"))

    actor, action, detail = audited[0]
    assert action == "git_token.refused"
    assert detail["ip"] == "10.0.0.5"


def test_a_refused_exchange_is_audited_with_the_proxy_address(monkeypatch, audited):
    monkeypatch.setattr(devices, "unverified_issuer", lambda _assertion: "dev_missing")
    monkeypatch.setattr(devices.Device, "objects", lambda **_: SimpleNamespace(first=lambda: None))

    with pytest.raises(errors.unauthorized.InvalidToken):
        devices.exchange(call_from("10.0.0.6"), None, ExchangeRequest(assertion="x"))

    actor, action, detail = audited[0]
    assert action == "token.refused"
    assert detail["ip"] == "10.0.0.6"
