"""The addresses the server hands out are the one the client used, passed on by the proxy. Nothing is configured."""

import contextvars
from unittest import mock

import pytest

from apiserver.idp import public_origin
from apiserver.idp.assertion import AssertionInvalid, make_assertion, verify_assertion
from apiserver.idp.keys import generate_private_key
from apiserver.idp.oidc import _same_realm

DEFAULTS = {
    "apiserver.idp.public_path": "/cairnml",
    "apiserver.idp.public_url": "",
    "apiserver.idp.web_url": "",
    "apiserver.idp.issuer": "http://cairnml-idp:8180/cairnml/idp/realms/cairnml",
    "apiserver.idp.web_client_id": "cairnml-web",
    "apiserver.storage.public_endpoint": "",
    "apiserver.storage.public_port": 9000,
    "apiserver.idp.trusted_proxies": ["127.0.0.0/8", "::1/128", "172.16.0.0/12"],
}
# An address of the Docker network, where the stack's proxy sits.
PROXY = "172.18.0.5"


class FakeConfig:
    def __init__(self, **overrides):
        self.values = {**DEFAULTS, **overrides}

    def get(self, key, default=None):
        return self.values.get(key, default)


def request(headers, host="apiserver:8008", remote_addr=PROXY, **config):
    """Runs in a context of its own, as a request does: what one request remembers the next does not see."""
    def run():
        with mock.patch.object(public_origin, "config", FakeConfig(**config)):
            public_origin.remember(headers, host, remote_addr)
            return {
                "api": public_origin.api_url(),
                "web": public_origin.web_url(),
                "issuer": public_origin.idp_issuer(),
                "storage": public_origin.storage_endpoint(),
                "logout": public_origin.idp_logout_url(),
            }
    return contextvars.Context().run(run)


@pytest.mark.parametrize("host", ["localhost", "192.168.1.20", "cairnml-box"])
def test_the_server_names_itself_after_the_address_the_client_used(host):
    urls = request({"X-Forwarded-Host": host, "X-Forwarded-Proto": "http"})

    assert urls["api"] == f"http://{host}/cairnml/api"
    assert urls["web"] == f"http://{host}/cairnml/"
    assert urls["issuer"] == f"http://{host}/cairnml/idp/realms/cairnml"
    assert urls["storage"] == f"http://{host}:9000"


def test_logout_ends_the_identity_providers_session_and_returns_to_the_sign_in():
    from urllib.parse import parse_qs, urlsplit

    url = urlsplit(request({"X-Forwarded-Host": "192.168.1.20"})["logout"])

    assert f"{url.scheme}://{url.netloc}{url.path}" == "http://192.168.1.20/cairnml/idp/realms/cairnml/protocol/openid-connect/logout"
    assert parse_qs(url.query) == {
        "client_id": ["cairnml-web"],
        "post_logout_redirect_uri": ["http://192.168.1.20/cairnml/api/v2.8/login.idp_start"],
    }


def test_the_id_token_of_the_sign_in_is_the_hint_that_skips_the_confirmation():
    from urllib.parse import parse_qs, urlsplit

    def logout(hint):
        def run():
            with mock.patch.object(public_origin, "config", FakeConfig()):
                public_origin.remember({"X-Forwarded-Host": "localhost"}, "x")
                return parse_qs(urlsplit(public_origin.idp_logout_url(hint)).query)
        return contextvars.Context().run(run)

    assert logout("the.id.token")["id_token_hint"] == ["the.id.token"]
    assert "id_token_hint" not in logout("")


def test_a_port_the_client_typed_is_kept_and_the_storage_port_is_the_configured_one():
    urls = request({"X-Forwarded-Host": "localhost:8081"}, **{"apiserver.storage.public_port": 9100})

    assert urls["api"] == "http://localhost:8081/cairnml/api"
    assert urls["issuer"] == "http://localhost:8081/cairnml/idp/realms/cairnml"
    assert urls["storage"] == "http://localhost:9100"


def test_without_the_proxy_the_host_of_the_request_is_used():
    assert request({}, host="127.0.0.1:18001")["api"] == "http://127.0.0.1:18001/cairnml/api"


def test_https_is_kept():
    urls = request({"X-Forwarded-Host": "example.org", "X-Forwarded-Proto": "https"})

    assert urls["api"] == "https://example.org/cairnml/api"
    assert urls["storage"] == "https://example.org:9000"


def test_a_value_in_the_configuration_wins():
    urls = request(
        {"X-Forwarded-Host": "localhost"},
        **{
            "apiserver.idp.public_url": "https://cairnml.example/api",
            "apiserver.idp.web_url": "https://cairnml.example/",
            "apiserver.storage.public_endpoint": "https://files.example",
        },
    )

    assert urls["api"] == "https://cairnml.example/api"
    assert urls["web"] == "https://cairnml.example/"
    assert urls["storage"] == "https://files.example"


def test_there_is_no_address_outside_a_request():
    with pytest.raises(RuntimeError):
        contextvars.Context().run(public_origin.origin)


@pytest.mark.parametrize("token_issuer", [
    "http://cairnml-idp:8180/cairnml/idp/realms/cairnml",
    "http://localhost/cairnml/idp/realms/cairnml",
    "https://192.168.1.20/cairnml/idp/realms/cairnml/",
])
def test_a_token_is_accepted_from_any_address_of_the_realm(token_issuer):
    assert _same_realm(token_issuer, "http://cairnml-idp:8180/cairnml/idp/realms/cairnml")


@pytest.mark.parametrize("token_issuer", [
    "http://localhost/cairnml/idp/realms/master",
    "http://localhost/other/idp/realms/cairnml",
    "ftp://localhost/cairnml/idp/realms/cairnml",
    "",
    None,
])
def test_a_token_of_another_realm_or_without_an_issuer_is_refused(token_issuer):
    assert not _same_realm(token_issuer, "http://cairnml-idp:8180/cairnml/idp/realms/cairnml")


PINNED = {"apiserver.idp.public_origin": "https://cairnml.example"}


@pytest.mark.parametrize("host", ["evil.example", "cairnml.example.evil", "192.168.1.20"])
def test_a_pinned_address_is_named_whatever_host_the_request_carried(host):
    urls = request({"X-Forwarded-Host": host, "X-Forwarded-Proto": "http"}, **PINNED)

    assert urls["api"] == "https://cairnml.example/cairnml/api"
    assert urls["web"] == "https://cairnml.example/cairnml/"
    assert urls["issuer"] == "https://cairnml.example/cairnml/idp/realms/cairnml"
    assert urls["storage"] == "https://cairnml.example:9000"


def test_a_pinned_address_is_named_without_forwarded_headers():
    urls = request({}, host="evil.example", **PINNED)

    assert urls["api"] == "https://cairnml.example/cairnml/api"


@pytest.mark.parametrize("host", ["evil.example", "cairnml.example.evil"])
def test_a_device_assertion_for_another_host_is_refused_when_the_address_is_pinned(host):
    # The device exchange checks the assertion's audience against api_url(): with a pinned address the audience of a
    # request for another host is still the pinned one, so an assertion addressed to that host fails.
    urls = request({"X-Forwarded-Host": host, "X-Forwarded-Proto": "https"}, **PINNED)
    key = generate_private_key()
    forged = make_assertion(key, "dev_1", f"https://{host}/cairnml/api")
    canonical = make_assertion(key, "dev_1", urls["api"])

    with pytest.raises(AssertionInvalid):
        verify_assertion(forged, key.public_key(), urls["api"])
    assert verify_assertion(canonical, key.public_key(), urls["api"])["iss"] == "dev_1"


def test_a_request_for_another_host_is_logged_under_a_pinned_address():
    with mock.patch.object(public_origin, "log") as log:
        request({"X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "https"}, **PINNED)

    log.warning.assert_called_once()


def test_a_request_for_the_pinned_host_is_not_logged():
    with mock.patch.object(public_origin, "log") as log:
        request({"X-Forwarded-Host": "CairnML.example", "X-Forwarded-Proto": "https"}, **PINNED)

    log.warning.assert_not_called()


@pytest.mark.parametrize("value, refused", [
    ("", False),
    ("https://cairnml.example", False),
    ("http://192.168.1.20:8081", False),
    ("cairnml.example", True),
    ("ftp://cairnml.example", True),
    ("https://cairnml.example/cairnml", True),
    ("https://cairnml.example:port", True),
])
def test_a_pinned_address_must_be_an_origin(value, refused):
    problems = public_origin.public_origin_problems(FakeConfig(**{"apiserver.idp.public_origin": value}).get)

    assert bool(problems) is refused


def test_forwarded_headers_from_an_untrusted_address_are_ignored_and_logged():
    with mock.patch.object(public_origin, "log") as log:
        urls = request({"X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "https"}, remote_addr="203.0.113.7")

    assert urls["api"] == "http://apiserver:8008/cairnml/api"
    assert urls["web"] == "http://apiserver:8008/cairnml/"
    assert urls["storage"] == "http://apiserver:9000"
    log.warning.assert_called_once()


def test_a_request_without_a_remote_address_does_not_trust_forwarded_headers():
    urls = request({"X-Forwarded-Host": "evil.example"}, remote_addr=None)

    assert urls["api"] == "http://apiserver:8008/cairnml/api"


def test_loopback_and_the_docker_network_are_trusted_by_default():
    for remote in ("127.0.0.1", "::1", "172.17.0.2", "172.31.255.254"):
        assert request({"X-Forwarded-Host": "localhost"}, remote_addr=remote)["api"] == "http://localhost/cairnml/api"


def test_an_address_outside_the_default_proxies_is_not_trusted():
    for remote in ("192.168.1.20", "8.8.8.8", "172.32.0.1", "not-an-address"):
        assert request({"X-Forwarded-Host": "evil.example"}, remote_addr=remote)["api"] == \
            "http://apiserver:8008/cairnml/api"


def test_an_ipv4_mapped_ipv6_address_of_a_trusted_proxy_is_trusted():
    urls = request({"X-Forwarded-Host": "localhost"}, remote_addr="::ffff:172.18.0.5")

    assert urls["api"] == "http://localhost/cairnml/api"


def test_a_configured_proxy_range_replaces_the_default():
    proxies = {"apiserver.idp.trusted_proxies": ["10.0.0.0/8"]}

    assert request({"X-Forwarded-Host": "lan.example"}, remote_addr="10.1.2.3", **proxies)["api"] == \
        "http://lan.example/cairnml/api"
    assert request({"X-Forwarded-Host": "lan.example"}, remote_addr="172.18.0.5", **proxies)["api"] == \
        "http://apiserver:8008/cairnml/api"


def test_a_pinned_address_still_wins_over_forwarded_headers_from_an_untrusted_address():
    urls = request({"X-Forwarded-Host": "evil.example"}, remote_addr="203.0.113.7", **PINNED)

    assert urls["api"] == "https://cairnml.example/cairnml/api"


@pytest.mark.parametrize("entry, refused", [
    ("10.0.0.0/8", False),
    ("192.0.2.1", False),
    ("::1/128", False),
    ("10.0.0.0/33", True),
    ("proxy.internal", True),
])
def test_a_trusted_proxy_must_be_an_address_or_network(entry, refused):
    problems = public_origin.trusted_proxies_problems(FakeConfig(**{"apiserver.idp.trusted_proxies": [entry]}).get)

    assert bool(problems) is refused
