"""Section 1 of docs/e2e_testing/E2E.md, the trust side of enrolment: what the server refuses, whatever the caller claims.

Each request here is made with the REST client, so the assertion can be built to be wrong in one way at a time.
"""

import time

import pytest

from support import client, settings, workspace
from support.api import ApiError, make_assertion


def _refused(exc_info):
    assert exc_info.value.status in (400, 401, 403), exc_info.value


def test_assertion_signed_with_another_key_is_refused(client_home):
    """An assertion signed with another key is refused.

    Expect: The exchange is refused with 400, 401 or 403, and no token is issued.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    device = client.device_id_of(client_home)
    forged = make_assertion(Ed25519PrivateKey.generate(), device, settings.API_URL)
    with pytest.raises(ApiError) as refused:
        workspace.device_api(client_home).call("devices.exchange", {"assertion": forged})
    _refused(refused)


def test_assertion_for_another_server_is_refused(client_home):
    """An assertion made for another server is refused.

    Expect: The exchange is refused with 400, 401 or 403.
    """
    api = workspace.device_api(client_home)
    wrong_audience = make_assertion(api.key, api.device_id, "http://127.0.0.1:1")
    with pytest.raises(ApiError) as refused:
        api.call("devices.exchange", {"assertion": wrong_audience})
    _refused(refused)


def test_expired_assertion_is_refused(client_home):
    """An expired assertion is refused.

    Expect: The exchange is refused with 400, 401 or 403.
    """
    api = workspace.device_api(client_home)
    expired = make_assertion(api.key, api.device_id, settings.API_URL, ttl=-600)
    with pytest.raises(ApiError) as refused:
        api.call("devices.exchange", {"assertion": expired})
    _refused(refused)


def test_assertion_cannot_be_used_twice(client_home):
    """An assertion is accepted once; replaying it is refused.

    Expect: The first exchange returns a token; the second, with the same assertion, is refused with 400, 401 or 403.
    """
    api = workspace.device_api(client_home)
    once = make_assertion(api.key, api.device_id, settings.API_URL)
    assert api.call("devices.exchange", {"assertion": once})["access_token"]
    with pytest.raises(ApiError) as replayed:
        api.call("devices.exchange", {"assertion": once})
    _refused(replayed)


def test_token_of_a_device_is_refused_once_the_user_is_disabled(tmp_path, member_account, keycloak):
    """A disabled account is refused when the directory sync has recorded it (the suite's stack syncs the directory every 5 s, compose.yaml).

    Expect: After the account is disabled, the device's token request is refused with 401 or 403 within about 5 s, the stack's sync interval (the wait allows 60 s).
    """
    home = tmp_path / "disabled-member"
    home.mkdir()
    assert client.init(home, member_account, name="e2e-disabled").returncode == 0
    api = workspace.device_api(home)
    assert api.token()
    keycloak.set_enabled(member_account[0], False)
    try:
        deadline = time.monotonic() + 60
        refused = False
        while time.monotonic() < deadline and not refused:
            try:
                api.token()
            except ApiError as exc:
                refused = exc.status in (401, 403)
                break
            time.sleep(1)
        assert refused, "a disabled account's device must be refused within the directory sync interval"
    finally:
        keycloak.set_enabled(member_account[0], True)
        client.logout(home)
