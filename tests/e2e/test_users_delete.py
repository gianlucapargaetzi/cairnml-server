"""users.delete, end to end: the user it deletes loses the access of their devices.

The call is internal (the roles system and root only), and no account of the stack has that role. The suite owns the
stack and its secrets, so it signs a token with a system identity itself, from a valid device token of the admin.
"""

import os

import jwt
import pytest

from support import client, settings, workspace
from support.api import ApiError


def _system_token(admin_home, stack_state) -> str:
    """The admin device's own valid token, signed again with the stack's token secret and a system identity."""
    claims = jwt.decode(workspace.device_api(admin_home).token(), options={"verify_signature": False})
    claims["identity"]["role"] = "system"
    secret = (stack_state / "secrets" / "token_secret").read_text().rstrip("\n")
    return jwt.encode(claims, secret, algorithm="HS256")


def test_the_device_of_a_deleted_user_is_refused(tmp_path, client_home, keycloak, isolated_stack):
    """The access of a user's device ends when the user is deleted.

    This guards #320 (the device is revoked) and #321 (the call completes) together, through the real call path. It
    passes with #320 alone: the deletion of the sign-in record is proven by the unit tests. There is no web-token leg,
    because the stack's directory sync recreates the account of a user who is still enabled within seconds.

    Expect: A throwaway user enrols a device and calls the API. After users.delete (called with a system identity) the
    token the device held is refused with 401 or 403, and so is a new exchange of the device's key.
    """
    username, password = f"{settings.PREFIX}-gone-{os.urandom(3).hex()}", os.urandom(12).hex()
    keycloak.create_user(username, password)
    home = tmp_path / "deleted-user"
    home.mkdir()
    try:
        assert client.init(home, (username, password), name="e2e-deleted").returncode == 0
        api = workspace.device_api(home)
        user_id = api.authed("users.get_current_user")["user"]["id"]
        assert user_id

        token = _system_token(client_home, isolated_stack.state)
        assert workspace.device_api(client_home).call("users.delete", {"user": user_id}, token=token) == {}

        with pytest.raises(ApiError) as held:
            api.authed("users.get_current_user")  # the token it held
        assert held.value.status in (401, 403), held.value
        with pytest.raises(ApiError) as exchanged:
            workspace.device_api(home).token()  # a new exchange, signed with the device's key
        assert exchanged.value.status in (400, 401, 403), exchanged.value
    finally:
        keycloak.delete_user(username)
        client.logout(home)
