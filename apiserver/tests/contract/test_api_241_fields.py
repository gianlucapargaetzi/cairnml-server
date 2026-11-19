"""The API 2.41 definitions of devices.git_token, tasks.update and tasks.enqueue declare the fields their handlers read
and return: the clone address in the git_token response, status_message in the tasks.update request and force in the
tasks.enqueue request. Before, the schema omitted them, so a client built from it could not send or read them.
"""

from apiserver.contract.check import SCHEMA_ROOT
from apiserver.contract.evolution import load_api
from apiserver.tests.contract.test_api_241_declarations import _request, _response

API = load_api(SCHEMA_ROOT)
CHANGED_ENDPOINTS = ("devices.git_token", "tasks.update", "tasks.enqueue")


def test_tasks_update_request_declares_status_message_at_241():
    assert _request("tasks.update", "2.41", "status_message")["type"] == "string"


def test_tasks_enqueue_request_declares_force_at_241():
    field = _request("tasks.enqueue", "2.41", "force")
    assert field["type"] == "boolean"
    assert field["default"] is False


def test_devices_git_token_response_declares_url_at_241():
    assert _response("devices.git_token", "2.41", "url")["type"] == "string"


def test_each_changed_endpoint_keeps_at_most_five_definitions():
    for name in CHANGED_ENDPOINTS:
        assert len(API[name].versions) <= 5, f"{name} has {len(API[name].versions)} definitions"
