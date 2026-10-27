"""Section 1 of docs/e2e_testing/E2E.md: enrolling a machine, checking it, re-enrolling and signing out.

Every enrolment here goes through cairnml init and the real sign-in page (headless browser).
"""

import os
import stat

import pytest

from support import cli, client, settings
from support.api import Api, ApiError


@pytest.fixture
def fresh_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return home


def test_init_enrols_the_machine_and_writes_its_key(client_home):
    """Enrolment through the sign-in page writes the device key and the configuration.

    Expect: The key file exists and is readable by its owner only (0600); the configuration names a device and the server.
    """
    key = client.key_path(client_home)
    assert key.is_file(), "the device key was not written"
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600, "the device key must be readable by its owner only"
    assert client.device_id_of(client_home).startswith("dev_"), "the configuration does not name the device"
    conf = (client_home / "cairnml.conf").read_text()
    assert settings.API_URL in conf, "the configuration does not name the server"


def test_whoami_names_the_admin_and_the_device(client_home, admin_account):
    """whoami shows the signed-in admin and the device.

    Expect: The command exits 0, and its output names the admin account and the device id.
    """
    result = client.whoami(client_home)
    assert result.returncode == 0, result.output
    assert admin_account[0] in result.stdout
    assert client.device_id_of(client_home) in result.stdout


def test_whoami_uses_the_device_key_without_a_browser(client_home):
    """whoami must not open a sign-in page: a browser command that fails the test if it is run.

    Expect: The command exits 0 and prints no sign-in prompt, with no browser available to it.
    """
    result = cli.run("cairnml", client_home, args=["whoami"], extra_env={"BROWSER": "/bin/false"})
    assert result.returncode == 0, result.output
    assert "sign" not in result.stdout.lower()


def test_init_again_without_force_is_refused(client_home, admin_account):
    """A second enrolment without --force is refused.

    Expect: The command fails with 'already enrolled', and the device key is unchanged.
    """
    before = client.key_path(client_home).read_bytes()
    result = client.init(client_home, admin_account)
    assert result.returncode != 0
    assert "already enrolled" in result.output
    assert client.key_path(client_home).read_bytes() == before, "a refused enrolment must keep the key"


def test_init_with_force_revokes_the_old_device(fresh_home, admin_account, session_home):
    """--force replaces the device and revokes the previous one.

    Expect: The new device gets a token; the old key is refused.
    """
    first = client.init(fresh_home, admin_account, name="e2e-first")
    assert first.returncode == 0, first.output
    old_device = client.device_id_of(fresh_home)
    old_key = client.key_path(fresh_home)
    saved_key = fresh_home / "old-device.key"
    saved_key.write_bytes(old_key.read_bytes())

    second = client.init(fresh_home, admin_account, name="e2e-second", force=True)
    assert second.returncode == 0, second.output
    assert "revoked the previous device" in second.output
    new_device = client.device_id_of(fresh_home)
    assert new_device and new_device != old_device

    # The old key must no longer get a token; the new one must.
    with pytest.raises(ApiError):
        Api(old_device, saved_key).token()
    assert Api(new_device, client.key_path(fresh_home)).token()


def test_logout_revokes_the_device_and_removes_its_key(fresh_home, admin_account):
    """logout revokes the device and removes its key.

    Expect: The key file is gone, and the old key is refused.
    """
    assert client.init(fresh_home, admin_account).returncode == 0
    device = client.device_id_of(fresh_home)
    key_path = client.key_path(fresh_home)
    saved_key = fresh_home / "saved.key"
    saved_key.write_bytes(key_path.read_bytes())

    result = client.logout(fresh_home)
    assert result.returncode == 0, result.output
    assert not key_path.exists(), "the device key must be removed"
    with pytest.raises(ApiError):
        Api(device, saved_key).token()


def test_whoami_after_logout_says_not_enrolled(fresh_home, admin_account):
    """whoami after logout says the machine is not enrolled.

    Expect: The command exits 1 and says 'not enrolled'.
    """
    assert client.init(fresh_home, admin_account).returncode == 0
    assert client.logout(fresh_home).returncode == 0
    result = client.whoami(fresh_home)
    assert result.returncode == 1
    assert "not enrolled" in result.output


def test_logout_on_a_machine_that_is_not_enrolled_does_nothing(fresh_home):
    """logout on a machine that is not enrolled does nothing.

    Expect: The command exits 0 and says 'nothing to sign out'.
    """
    result = client.logout(fresh_home)
    assert result.returncode == 0
    assert "nothing to sign out" in result.output


def test_init_with_an_unreachable_server_writes_nothing(fresh_home, admin_account):
    """An enrolment against an unreachable server writes nothing.

    Expect: The command fails with 'cannot reach' and no key file is written.
    """
    result = client.init(fresh_home, admin_account, server="http://127.0.0.1:9", name="e2e-nowhere")
    assert result.returncode != 0
    assert "cannot reach" in result.output
    assert not client.key_path(fresh_home).exists()


def test_init_takes_the_servers_address_and_finds_the_api(fresh_home, admin_account):
    """The server is one address; the API is under /cairnml/api of it. The address may be given without the path.

    Expect: The configuration names the API address.
    """
    server = settings.WEB_URL[: -len("/cairnml")]
    result = client.init(fresh_home, admin_account, server=server, name="e2e-web-address")
    assert result.returncode == 0, result.output
    assert settings.API_URL in (fresh_home / "cairnml.conf").read_text()
    assert client.logout(fresh_home).returncode == 0


def test_member_enrols_as_a_plain_user(fresh_home, member_account):
    """A plain user can enrol a machine.

    Expect: Enrolment succeeds; whoami names the user, who is not an admin.
    """
    result = client.init(fresh_home, member_account, name="e2e-member-laptop")
    assert result.returncode == 0, result.output
    who = client.whoami(fresh_home)
    assert who.returncode == 0, who.output
    assert member_account[0] in who.stdout
    assert "admin" not in who.stdout.split("(")[1].split(")")[0].lower()
    assert client.logout(fresh_home).returncode == 0
