from apiserver.config_repo import config
from apiserver.services import devices


def _settings(monkeypatch, app):
    """The flag alone: the addresses come from the request, which these unit tests do not serve. ``app`` stands for the
    server's GitHub App stored by an admin, or None when none is added."""
    monkeypatch.setattr(devices.public_origin, "idp_issuer", lambda: "http://x/idp")
    monkeypatch.setattr(devices.public_origin, "api_url", lambda: "http://x/cairnml/api")
    monkeypatch.setattr(devices.public_origin, "web_url", lambda: "http://x/cairnml/")
    monkeypatch.setattr(devices.user_access, "server_app", lambda: app)
    return devices.idp_settings(None, None, None)


def test_the_settings_say_a_github_app_is_configured(monkeypatch):
    body = _settings(monkeypatch, (object(), "KEY-MATERIAL"))
    assert body["github_app"] is True
    assert "KEY-MATERIAL" not in str(body)


def test_the_settings_say_no_github_app_is_configured(monkeypatch):
    assert _settings(monkeypatch, None)["github_app"] is False
