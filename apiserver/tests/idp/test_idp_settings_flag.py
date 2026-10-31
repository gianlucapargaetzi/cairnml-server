from apiserver.config_repo import config
from apiserver.services import devices


def _settings(monkeypatch, app_id):
    """The flag alone: the addresses come from the request, which these unit tests do not serve."""
    monkeypatch.setattr(devices.public_origin, "idp_issuer", lambda: "http://x/idp")
    monkeypatch.setattr(devices.public_origin, "api_url", lambda: "http://x/cairnml/api")
    monkeypatch.setattr(devices.public_origin, "web_url", lambda: "http://x/cairnml/")
    original = config.get
    monkeypatch.setattr(config, "get", lambda k, d=None: app_id if k == "apiserver.idp.github_app_id" else original(k, d))
    return devices.idp_settings(None, None, None)


def test_the_settings_say_a_github_app_is_configured(monkeypatch):
    body = _settings(monkeypatch, "123456")
    assert body["github_app"] is True
    assert "123456" not in str(body)


def test_the_settings_say_no_github_app_is_configured(monkeypatch):
    assert _settings(monkeypatch, "")["github_app"] is False
