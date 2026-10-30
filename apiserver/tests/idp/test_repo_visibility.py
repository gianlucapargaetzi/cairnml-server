import socket
from unittest import TestCase, mock

import requests

from apiserver.apierrors import errors
from apiserver.idp import repo_visibility
from apiserver.idp.repo_visibility import check_readable, is_private

PUBLIC = "93.184.216.34"  # a public address (example.com), used with the resolver mocked
GIT_ADVERTISEMENT = "application/x-git-upload-pack-advertisement"
REPO = "https://gitlab.example.org/team/proj"


def response(status, content_type=GIT_ADVERTISEMENT):
    return mock.Mock(status_code=status, headers={"Content-Type": content_type})


def resolving(*addresses):
    """A resolver mock answering with the given addresses, in getaddrinfo's shape."""
    infos = []
    for address in addresses:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sockaddr = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
        infos.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
    return mock.patch.object(repo_visibility.socket, "getaddrinfo", return_value=infos)


class TestAddressRules(TestCase):
    BLOCKED = [
        "127.0.0.1", "127.255.0.1",          # loopback
        "169.254.169.254",                     # link-local, cloud metadata
        "10.1.2.3", "172.16.0.5", "172.31.255.1", "192.168.1.9",  # RFC1918 (includes Docker bridge networks)
        "100.64.0.1",                           # carrier-grade NAT
        "0.0.0.0", "224.0.0.1",
        "::1", "fe80::1", "fc00::5", "fd12:3456::9",  # loopback, link-local, unique-local
        "::ffff:10.0.0.7", "::ffff:127.0.0.1",   # IPv4-mapped private addresses
    ]

    def test_loopback_link_local_private_and_unique_local_addresses_are_blocked(self):
        for address in self.BLOCKED:
            with self.subTest(address=address):
                with resolving(address):
                    self.assertIsNone(is_private(REPO))

    def test_the_probe_is_not_sent_to_a_blocked_address(self):
        for address in ("127.0.0.1", "169.254.169.254", "10.0.0.4", "fd00::4"):
            with self.subTest(address=address), resolving(address), \
                    mock.patch.object(repo_visibility.requests, "get") as get:
                is_private(REPO)
            get.assert_not_called()

    def test_one_blocked_address_among_public_ones_blocks_the_probe(self):
        with resolving(PUBLIC, "10.0.0.4"), mock.patch.object(repo_visibility.requests, "get") as get:
            self.assertIsNone(is_private(REPO))
        get.assert_not_called()

    def test_a_public_address_is_probed(self):
        with resolving(PUBLIC), fetching(response(200)) as fetch:
            self.assertIs(is_private(REPO), False)
        fetch.assert_called_once()

    def test_a_configured_network_is_blocked_too(self):
        with mock.patch.object(repo_visibility, "config") as cfg:
            cfg.get.return_value = ["198.51.100.0/24"]
            with resolving("198.51.100.7"), mock.patch.object(repo_visibility.requests, "get") as get:
                self.assertIsNone(is_private(REPO))
        get.assert_not_called()

    def test_a_name_that_does_not_resolve_gives_no_answer(self):
        with mock.patch.object(repo_visibility.socket, "getaddrinfo", side_effect=socket.gaierror("no such host")):
            self.assertIsNone(is_private(REPO))


def fetching(response_or_error):
    """Mocks the one request the probe makes. The mock returns the response, or raises the error."""
    if isinstance(response_or_error, Exception):
        return mock.patch.object(repo_visibility, "_fetch", side_effect=response_or_error)
    return mock.patch.object(repo_visibility, "_fetch", return_value=response_or_error)


class TestIsPrivate(TestCase):
    def test_an_anonymous_advertisement_means_public(self):
        with resolving(PUBLIC), fetching(response(200)) as fetch:
            self.assertIs(is_private("https://gitlab.example.org/team/proj.git"), False)
        self.assertEqual(fetch.call_args[0], (PUBLIC, "https", 443, "team/proj", "gitlab.example.org",
                                              "gitlab.example.org"))

    def test_the_request_connects_to_the_checked_address_and_keeps_the_name(self):
        with resolving(PUBLIC), fetching(response(200)) as fetch:
            is_private(REPO)
        address, scheme, port, path, name, host_header = fetch.call_args[0]
        self.assertEqual(address, PUBLIC)
        self.assertEqual(name, "gitlab.example.org")
        self.assertEqual(host_header, "gitlab.example.org")

    def test_a_second_resolution_to_a_blocked_address_does_not_change_the_outcome(self):
        """The name is resolved once and the request connects to the address checked. A later answer that points
        at internal space is never asked for, so it cannot redirect the request."""
        rebinding = [
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 443))],
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.4", 443))],
        ]
        with mock.patch.object(repo_visibility.socket, "getaddrinfo", side_effect=rebinding) as getaddrinfo, \
                fetching(response(200)) as fetch:
            self.assertIs(is_private(REPO), False)
        getaddrinfo.assert_called_once()
        self.assertEqual(fetch.call_args[0][0], PUBLIC)

    def test_a_blocked_answer_on_the_only_resolution_gives_no_answer_and_no_request(self):
        with resolving(PUBLIC, "10.0.0.4"), fetching(response(200)) as fetch:
            self.assertIsNone(is_private(REPO))
        fetch.assert_not_called()

    def test_the_proxy_environment_is_not_used(self):
        seen = {}

        class FakeSession:
            def mount(self, prefix, adapter):
                seen["adapter"] = adapter

            def get(self, url, **kwargs):
                seen["url"], seen["kwargs"], seen["trust_env"] = url, kwargs, self.trust_env
                return response(200)

            def close(self):
                pass

        with mock.patch.object(repo_visibility.requests, "Session", FakeSession):
            repo_visibility._fetch(PUBLIC, "https", 443, "team/proj", "gitlab.example.org", "gitlab.example.org")
        self.assertIs(seen["trust_env"], False)
        self.assertEqual(seen["url"], f"https://{PUBLIC}:443/team/proj/info/refs?service=git-upload-pack")
        self.assertEqual(seen["kwargs"]["headers"], {"Host": "gitlab.example.org"})
        self.assertIs(seen["kwargs"]["allow_redirects"], False)
        self.assertEqual(seen["adapter"]._name, "gitlab.example.org")

    def test_the_tls_name_check_uses_the_host_name_not_the_address(self):
        adapter = repo_visibility._PinnedAdapter("gitlab.example.org", tls=True)
        adapter.init_poolmanager(1, 1)
        pool = adapter.poolmanager.connection_from_url(f"https://{PUBLIC}:443/")
        self.assertEqual(adapter.poolmanager.connection_pool_kw["server_hostname"], "gitlab.example.org")
        self.assertEqual(pool.host, PUBLIC)

    def test_a_plain_http_probe_is_not_given_a_tls_name(self):
        adapter = repo_visibility._PinnedAdapter("gitlab.example.org", tls=False)
        adapter.init_poolmanager(1, 1)
        pool = adapter.poolmanager.connection_from_url(f"http://{PUBLIC}:80/")
        self.assertEqual(pool.host, PUBLIC)
        self.assertNotIn("server_hostname", adapter.poolmanager.connection_pool_kw)

    def test_a_redirect_gives_no_answer(self):
        with resolving(PUBLIC), fetching(response(302)):
            self.assertIsNone(is_private(REPO))

    def test_the_repository_path_keeps_its_case(self):
        with resolving(PUBLIC), fetching(response(200)) as fetch:
            is_private("https://gitlab.example.org/Team/Proj")
        self.assertEqual(fetch.call_args[0][3], "Team/Proj")

    def test_a_refused_anonymous_read_means_private(self):
        for status in (401, 403, 404):
            with self.subTest(status=status), resolving(PUBLIC), fetching(response(status)):
                self.assertIs(is_private(REPO), True)

    def test_a_sign_in_page_instead_of_an_advertisement_means_private(self):
        with resolving(PUBLIC), fetching(response(200, "text/html")):
            self.assertIs(is_private(REPO), True)

    def test_a_host_that_cannot_be_asked_gives_no_answer(self):
        with resolving(PUBLIC), fetching(requests.ConnectionError("down")):
            self.assertIsNone(is_private(REPO))
        with resolving(PUBLIC), fetching(response(503)):
            self.assertIsNone(is_private(REPO))

    def test_urls_that_are_not_http_are_not_asked(self):
        with fetching(response(200)) as fetch:
            self.assertIsNone(is_private("git@gitlab.example.org:team/proj.git"))
            self.assertIsNone(is_private("ssh://gitlab.example.org/team/proj"))
        fetch.assert_not_called()

    def test_user_information_in_the_url_is_not_sent_to_the_host(self):
        with resolving(PUBLIC), fetching(response(200)) as fetch:
            is_private("https://user:secret@gitlab.example.org/team/proj")
        self.assertNotIn("secret", str(fetch.call_args))


class TestCheckReadable(TestCase):
    def test_a_public_repository_is_accepted_without_credentials(self):
        with mock.patch.object(repo_visibility, "is_private", return_value=False), \
                mock.patch.object(repo_visibility.store, "find_credential") as find:
            check_readable("c1", REPO)
        find.assert_not_called()

    def test_a_host_that_cannot_be_asked_is_refused_without_credentials(self):
        with mock.patch.object(repo_visibility, "is_private", return_value=None), \
                mock.patch.object(repo_visibility.store, "find_credential", return_value=None):
            with self.assertRaises(errors.bad_request.ValidationError) as ctx:
                check_readable("c1", REPO)
        self.assertIn("gitlab.example.org", str(ctx.exception))

    def test_a_blocked_address_is_refused_like_an_unreachable_host(self):
        with resolving("10.0.0.4"), mock.patch.object(repo_visibility.store, "find_credential", return_value=None):
            with self.assertRaises(errors.bad_request.ValidationError):
                check_readable("c1", REPO)

    def test_a_private_repository_without_credentials_is_refused_naming_the_host(self):
        with mock.patch.object(repo_visibility, "is_private", return_value=True), \
                mock.patch.object(repo_visibility.store, "find_credential", return_value=None):
            with self.assertRaises(errors.bad_request.ValidationError) as ctx:
                check_readable("c1", REPO)
        self.assertIn("gitlab.example.org", str(ctx.exception))
        self.assertIn("git_credentials.set", str(ctx.exception))

    def test_a_private_repository_with_stored_credentials_is_accepted(self):
        with mock.patch.object(repo_visibility, "is_private", return_value=True), \
                mock.patch.object(repo_visibility.store, "find_credential", return_value=object()):
            check_readable("c1", REPO)

    def test_an_unreachable_host_with_stored_credentials_is_accepted(self):
        with mock.patch.object(repo_visibility, "is_private", return_value=None), \
                mock.patch.object(repo_visibility.store, "find_credential", return_value=object()):
            check_readable("c1", REPO)
