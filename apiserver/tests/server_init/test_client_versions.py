from unittest import mock

from apiserver.server_init import client_versions
from apiserver.server_init.client_versions import (
    COMPATIBILITY_DOCS,
    note,
    outdated,
    parse_header,
    parse_version,
    upgrade_notice,
)

MINIMA = {"min_sdk_version": "0.1.0", "min_agent_version": "0.0.5"}


def minima(key, default=""):
    return MINIMA.get(key, default)


class TestParsing(object):
    def test_versions_compare_as_numbers(self):
        assert parse_version("0.10.0") > parse_version("0.9.9")
        assert parse_version("1.2") == (1, 2)

    def test_text_that_is_not_a_version_is_none(self):
        assert parse_version("") is None
        assert parse_version("1.2-beta") is None
        assert parse_version(None) is None

    def test_header_entries_are_name_and_version(self):
        assert parse_header("cairnml-0.0.2, cairnml_agent-0.0.1") == [("cairnml", "0.0.2"), ("cairnml_agent", "0.0.1")]
        assert parse_header("cairnml-agent-0.0.1") == [("cairnml-agent", "0.0.1")]

    def test_entries_without_a_version_are_ignored(self):
        assert parse_header("curl, -1.0") == []
        assert parse_header("") == []


class TestOutdatedClients(object):
    def test_an_sdk_below_the_minimum_is_reported(self):
        with mock.patch.object(client_versions, "_minimum", side_effect=minima):
            assert outdated("cairnml-0.0.9") == [("cairnml", "0.0.9", "0.1.0")]

    def test_an_agent_below_its_minimum_is_reported_under_either_name(self):
        with mock.patch.object(client_versions, "_minimum", side_effect=minima):
            assert outdated("cairnml_agent-0.0.4") == [("cairnml_agent", "0.0.4", "0.0.5")]
            assert outdated("cairnml-agent-0.0.4") == [("cairnml-agent", "0.0.4", "0.0.5")]

    def test_clients_at_or_above_the_minimum_are_not_reported(self):
        with mock.patch.object(client_versions, "_minimum", side_effect=minima):
            assert outdated("cairnml-0.1.0, cairnml_agent-0.0.5") == []
            assert outdated("cairnml-2.4.0") == []

    def test_other_clients_and_missing_headers_are_not_reported(self):
        with mock.patch.object(client_versions, "_minimum", side_effect=minima):
            assert outdated("curl-7.0") == []
            assert outdated(None) == []

    def test_no_minimum_configured_reports_nothing(self):
        with mock.patch.object(client_versions, "_minimum", return_value=""):
            assert outdated("cairnml-0.0.1") == []


class TestUpgradeNotice(object):
    def test_the_notice_names_the_client_and_points_to_the_compatibility_section(self):
        with mock.patch.object(client_versions, "_minimum", side_effect=minima):
            notice = upgrade_notice({"X-CairnML-Client": "cairnml-0.0.9"})
        assert "the SDK (cairnml) 0.0.9 is older than this server's minimum 0.1.0" in notice
        assert notice.endswith(COMPATIBILITY_DOCS)

    def test_the_legacy_header_name_is_read_too(self):
        with mock.patch.object(client_versions, "_minimum", side_effect=minima):
            assert upgrade_notice({"X-Trains-Client": "cairnml-0.0.1"}) is not None

    def test_a_current_client_gets_no_notice(self):
        with mock.patch.object(client_versions, "_minimum", side_effect=minima):
            assert upgrade_notice({"X-CairnML-Client": "cairnml-0.1.0"}) is None
            assert upgrade_notice({}) is None

    def test_the_same_notice_is_logged_once(self):
        client_versions._logged.clear()
        with mock.patch.object(client_versions, "_minimum", side_effect=minima), \
                mock.patch.object(client_versions.log, "warning") as warning:
            note({"X-CairnML-Client": "cairnml-0.0.9"})
            note({"X-CairnML-Client": "cairnml-0.0.9"})
        assert warning.call_count == 1
        client_versions._logged.clear()

    def test_the_shipped_minimum_does_not_refuse_the_current_sdk_or_agent(self):
        # The versions the server's pins name at this commit: cairnml 0.0.2 and cairnml-agent 0.0.1.
        assert client_versions._minimum("min_sdk_version") == "0.0.2"
        assert client_versions._minimum("min_agent_version") == "0.0.1"
        assert upgrade_notice({"X-CairnML-Client": "cairnml-0.0.2, cairnml_agent-0.0.1"}) is None


class TestResponseHeader(object):
    def test_a_response_to_an_outdated_client_carries_the_upgrade_header(self):
        from flask import Flask

        from apiserver.server_init.request_handlers import RequestHandlers

        app = Flask(__name__)

        @app.route("/debug.ping")
        def ping():
            return "pong"

        app.after_request(RequestHandlers().after_request)
        with mock.patch.object(client_versions, "_minimum", side_effect=minima):
            old = app.test_client().get("/debug.ping", headers={"X-CairnML-Client": "cairnml-0.0.9"})
            current = app.test_client().get("/debug.ping", headers={"X-CairnML-Client": "cairnml-0.1.0"})
        assert "is older than this server's minimum 0.1.0" in old.headers[client_versions.UPGRADE_HEADER]
        assert client_versions.UPGRADE_HEADER not in current.headers
        assert old.status_code == 200  # the old client is served, not refused
