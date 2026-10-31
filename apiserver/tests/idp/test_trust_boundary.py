"""Trust boundaries that need no services: the cookie rule over HTTPS, the endpoints that answer without a login, and the
limits the proxy puts in front of the API."""

from pathlib import Path

import pytest

from apiserver.idp.legacy import cookie_settings_problems
from apiserver.schema import SchemaReader

PROXY_TEMPLATE = Path(__file__).resolve().parents[3] / "docker/build/internal_files/cairnml.conf.template"

# Every endpoint that does not require a login (authorize false or null), with the reason. A new one must be added here
# on purpose, so that it is reviewed. The rule is in the issue #30: the bootstrap endpoints only.
PUBLIC_ENDPOINTS = {
    "login.supported_modes": "optional: lists the sign-in modes; reveals no account",
    "login.logout": "optional: works without a session, and returns only where to end the provider's session",
    "login.idp_start": "starts the sign-in with the identity provider",
    "login.idp_callback": "finishes the sign-in; the provider's code and state are checked",
    "devices.idp_settings": "the issuer and the client id devices sign in with (public information)",
    "devices.enroll": "registers a device key; checks the identity provider's token itself",
    "devices.exchange": "exchanges a signed device assertion for a token; checks the signature itself",
    "auth.fixed_users_mode": "reports whether fixed users are on (they are removed; the answer is a status)",
    "debug.ping": "liveness probe",
}
OPTIONAL_ENDPOINTS = {"login.supported_modes", "login.logout"}


def _endpoint_policies():
    """(full name, authorize) for every endpoint group, read from the schema files without writing a cache."""
    import tempfile

    reader = SchemaReader()
    reader.cache_path = Path(tempfile.mkdtemp()) / "_cache.json"
    schema = reader.get_schema()
    for service in schema.services.values():
        for action, group in service.endpoint_groups.items():
            yield f"{service.name}.{action}", group.authorize


def test_the_endpoints_without_a_login_are_exactly_the_listed_ones():
    public = {name for name, authorize in _endpoint_policies() if not authorize and authorize is not None}
    optional = {name for name, authorize in _endpoint_policies() if authorize is None}

    assert public | optional == set(PUBLIC_ENDPOINTS)
    assert optional == OPTIONAL_ENDPOINTS


def test_every_other_endpoint_requires_a_login():
    protected = {name for name, authorize in _endpoint_policies() if authorize is True}

    assert protected
    assert not protected & set(PUBLIC_ENDPOINTS)


class TestSecureCookieOverHttps:
    @staticmethod
    def problems(**addresses):
        values = {"apiserver.auth.cookies.secure": addresses.pop("secure", False), **addresses}
        return cookie_settings_problems(lambda key, default=None: values.get(key, default))

    @pytest.mark.parametrize("address", [
        "apiserver.idp.public_origin", "apiserver.idp.public_url", "apiserver.idp.web_url",
    ])
    def test_an_https_address_in_any_setting_needs_a_secure_cookie(self, address):
        assert len(self.problems(**{address: "https://cairnml.example", "secure": False})) == 1

    @pytest.mark.parametrize("address", [
        "apiserver.idp.public_origin", "apiserver.idp.public_url", "apiserver.idp.web_url",
    ])
    def test_an_https_address_with_a_secure_cookie_starts(self, address):
        assert self.problems(**{address: "https://cairnml.example", "secure": True}) == []

    def test_plain_http_addresses_may_use_an_insecure_cookie(self):
        assert self.problems(**{"apiserver.idp.public_origin": "http://192.168.1.20:8081"}) == []


class TestProxyLimits:
    """The limits of the sign-in and device calls and of request bodies are set at the proxy. The API port is not
    published, so the proxy is the only way in: its limits apply to every client."""

    @staticmethod
    def blocks():
        text = PROXY_TEMPLATE.read_text(encoding="utf-8")
        return [block for block in text.split("location ")[1:]]

    def test_the_sign_in_and_device_calls_are_limited_to_five_per_second_per_address(self):
        text = PROXY_TEMPLATE.read_text(encoding="utf-8")

        assert "limit_req_zone $binary_remote_addr zone=cairnml_unauthenticated:10m rate=5r/s;" in text
        limited = [block for block in self.blocks() if "(login|devices)" in block.split("\n", 1)[0]]
        assert len(limited) == 1
        assert "limit_req zone=cairnml_unauthenticated burst=20 nodelay;" in limited[0]

    def test_the_limited_calls_answer_too_many_requests(self):
        assert "limit_req_status 429;" in PROXY_TEMPLATE.read_text(encoding="utf-8")

    def test_api_bodies_are_limited_to_ten_megabytes(self):
        text = PROXY_TEMPLATE.read_text(encoding="utf-8")

        assert "client_max_body_size 10m;" in text

    def test_only_the_event_upload_has_no_body_limit(self):
        unlimited = [block for block in self.blocks() if "client_max_body_size 0;" in block]

        assert len(unlimited) == 1
        assert "events\\.add_batch" in unlimited[0].split("\n", 1)[0]
