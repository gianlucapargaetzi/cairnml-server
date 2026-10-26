"""Section 8 of docs/e2e_testing/E2E.md: deleting what the suite made. The stack itself is removed at the end of the session (conftest).

The bytes of a deleted run are removed from object storage by the asynchronous delete service, so the check waits
for that service rather than asking once.
"""

import time
from urllib.parse import unquote

import pytest
import requests

from support import workspace
from support.workspace import new_name, run_json

def test_deleted_run_loses_its_stored_bytes(client_home, project, tmp_path):
    """A deleted run loses its stored bytes.

    Expect: The delete service finishes the deletion (its record for the url goes, without a failure), and the artifact's
    download link no longer serves the bytes.
    """
    tmp_dir = tmp_path
    api = workspace.device_api(client_home)
    run = run_json(client_home, "artifact_upload.py", [
        "--project", project, "--name", new_name("to-delete"), "--work", str(tmp_dir / "sources"),
        "--expect", str(tmp_dir / "expect.json"),
    ], timeout=300)
    task_id = run["task_id"]
    record = api.authed("tasks.get_by_id", {"task": task_id})["task"]
    uri = next(a["uri"] for a in record["execution"]["artifacts"] if a["key"] == "file")
    kind, _, name = (unquote(part) for part in uri[len("datachunk://"):].split("/", 2))
    link = api.authed("datachunks.prepare_download", {"task": task_id, "kind": kind, "name": name})["url"]
    assert requests.get(link, timeout=60).status_code == 200, "the artifact must be readable before the delete"

    api.authed("tasks.delete", {"task": task_id, "force": True, "return_file_urls": False})
    from support.api import ApiError

    with pytest.raises(ApiError):
        api.authed("tasks.get_by_id", {"task": task_id})

    # The delete service's own state: its record for the url stays until the bytes are gone. A failed attempt says why.
    deadline = time.monotonic() + 60
    pending = []
    while time.monotonic() < deadline:
        pending = api.authed("system.e2e_deletions", {"urls": [uri]})["pending"]
        if not pending or pending[0]["status"] == "failed":
            break
        time.sleep(1)
    assert not [p for p in pending if p["status"] == "failed"], f"the delete service failed: {pending}"
    assert not pending, f"the delete service has not finished after 60 s: {pending}"
    # and the bytes themselves: the link must no longer serve them
    assert requests.get(link, timeout=60).status_code != 200, "the deleted run's bytes must leave object storage"


def test_datachunk_route_refuses_a_deleted_run(client_home, project):
    """The download route refuses a deleted run.

    Expect: The route answers 400 or 404 for the deleted run's artifact.
    """
    from support.api import ApiError

    api = workspace.device_api(client_home)
    run = run_json(client_home, "track_example.py", ["--project", project, "--name", new_name("gone"), "--steps", "1"])
    api.authed("tasks.delete", {"task": run["task_id"], "force": True})
    with pytest.raises(ApiError) as refused:
        api.authed("datachunks.prepare_download", {"task": run["task_id"], "kind": "artifact", "name": "file"})
    assert refused.value.status in (400, 404)
