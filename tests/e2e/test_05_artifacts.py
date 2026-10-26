"""Section 2 of docs/e2e_testing/E2E.md: artifacts of every kind, read back on another machine, with their hashes and metadata.

The upload runs once per module, in one task. Every check then reads the task back in a new process, so the
content is what the server stored, not what the uploading process still has in memory.
"""

import hashlib
import json
import uuid
from urllib.parse import unquote

import pytest
import requests

from support import workspace
from support import settings
from support.workspace import new_name, run_json

KINDS = ["file", "folder", "dict", "frame", "array", "image", "pickled", "with_metadata"]


@pytest.fixture(scope="module")
def uploaded(client_home, project, tmp_path_factory):
    work = tmp_path_factory.mktemp("artifacts")
    expect = work / "expect.json"
    upload = run_json(client_home, "artifact_upload.py", [
        "--project", project, "--name", new_name("artifacts"),
        "--work", str(work / "sources"), "--expect", str(expect),
    ])
    check = run_json(client_home, "artifact_check.py", ["--task-id", upload["task_id"], "--expect", str(expect)])
    return {"task_id": upload["task_id"], "check": check, "expect": json.loads(expect.read_text()), "work": work}


@pytest.mark.parametrize("kind", KINDS)
def test_artifact_content_matches_after_read_back(uploaded, kind):
    """Each kind of artifact reads back with the content uploaded.

    Expect: On another process, the content read back equals the content uploaded, for each kind.
    """
    result = uploaded["check"]["results"][kind]
    assert result["ok"], f"{kind}: the content read back differs from the content uploaded: {result}"


def test_every_uploaded_artifact_is_listed_on_the_task(uploaded):
    """Every uploaded artifact is listed on the task.

    Expect: All eight kinds are listed as artifact names of the task.
    """
    assert set(KINDS) <= set(uploaded["check"]["names"])


def test_file_artifact_records_its_sha256(uploaded):
    """The file artifact records its sha256.

    Expect: The recorded hash equals the sha256 of the uploaded bytes.
    """
    recorded = uploaded["check"]["results"]["file"]["hash"]
    expected = uploaded["expect"]["artifacts"]["file"][1]
    assert recorded == expected, "the stored hash must be the sha256 of the uploaded bytes"


def test_metadata_is_stored_and_returned(uploaded):
    """Artifact metadata is stored and returned.

    Expect: The metadata read back equals the metadata uploaded.
    """
    metadata = uploaded["check"]["results"]["with_metadata"]["metadata"]
    assert metadata == {"owner": "e2e", "kind": "metadata"}


def test_artifacts_without_metadata_have_none(uploaded):
    """An artifact uploaded without metadata has none.

    Expect: The metadata read back is empty.
    """
    assert not uploaded["check"]["results"]["dict"]["metadata"]


@pytest.mark.slow
def test_large_file_uploads_and_downloads_intact(client_home, project, tmp_path):
    """Above the SDK's multipart threshold (8 MB by default): a 10 MB file goes up in parts.

    Expect: a 10 MB file uploaded in parts reads back intact, with the sha256 it was uploaded with.
    """
    expect = tmp_path / "expect.json"
    upload = run_json(client_home, "artifact_upload.py", [
        "--project", project, "--name", new_name("large"), "--work", str(tmp_path / "sources"),
        "--expect", str(expect), "--big-mb", "10",
    ], timeout=300)
    check = run_json(client_home, "artifact_check.py", ["--task-id", upload["task_id"], "--expect", str(expect)],
                     timeout=300)
    assert check["results"]["big"]["ok"], check["results"]["big"]


def test_download_link_serves_the_stored_bytes_from_object_storage(client_home, uploaded):
    """The record holds a datachunk; the server answers with a presigned storage link, and the bytes behind it
    have the sha256 that was uploaded.

    Expect: The link points at the storage service, and its bytes have the sha256 that was uploaded.
    """
    api = workspace.device_api(client_home)
    record = api.authed("tasks.get_by_id", {"task": uploaded["task_id"]})["task"]
    uri = next(a["uri"] for a in record["execution"]["artifacts"] if a["key"] == "file")
    kind, task_id, name = (unquote(part) for part in uri[len("datachunk://"):].split("/", 2))
    assert kind == "artifact" and task_id == uploaded["task_id"]
    link = api.authed("datachunks.prepare_download", {"task": task_id, "kind": kind, "name": name})
    url = link.get("url")
    assert url and url.startswith(settings.STORAGE_URL), f"the link must point at the storage service: {link}"
    body = requests.get(url, timeout=60)
    assert body.status_code == 200
    assert hashlib.sha256(body.content).hexdigest() == uploaded["expect"]["artifacts"]["file"][1]


def test_a_member_who_is_not_the_owner_cannot_delete_the_run(client_home, member_account, project, tmp_path):
    """Object access follows the project: a plain user with no role on the admin's private project may not change its runs.

    The member is not on the project, so the delete is refused exactly as a genuinely missing task is: the same 400
    and the same message, not a 401 or 403 that would reveal the task and its project exist.

    Expect: the member's delete of the owner's task and of a missing task are refused with the same 400 response.
    """
    from support import client
    from support.api import Api, ApiError

    owner_run = run_json(client_home, "track_example.py", ["--project", project, "--name", new_name("owned"),
                                                           "--steps", "1"])
    home = tmp_path / "member-home"
    home.mkdir()
    result = client.init(home, member_account, name="e2e-member")
    assert result.returncode == 0, result.output
    member_api = Api(client.device_id_of(home), client.key_path(home))
    try:
        with pytest.raises(ApiError) as refused:
            member_api.authed("tasks.delete", {"task": owner_run["task_id"], "force": True})
        missing_id = uuid.uuid4().hex
        with pytest.raises(ApiError) as missing:
            member_api.authed("tasks.delete", {"task": missing_id, "force": True})
        assert refused.value.status == missing.value.status == 400
        # the two messages differ only in the id each request named
        assert refused.value.message.replace(owner_run["task_id"], "<id>") == \
            missing.value.message.replace(missing_id, "<id>")
    finally:
        client.logout(home)


def test_a_downloaded_artifact_is_identical_on_a_second_download(client_home, uploaded):
    """Two downloads of the same artifact give the same bytes, and the sha256 in the record matches them.

    Expect: Both checks pass, and both hashes equal the hash recorded in the task.
    """
    expect = uploaded["work"] / "expect.json"
    first = run_json(client_home, "artifact_check.py", ["--task-id", uploaded["task_id"], "--expect", str(expect)])
    second = run_json(client_home, "artifact_check.py", ["--task-id", uploaded["task_id"], "--expect", str(expect)])
    assert first["results"]["file"]["ok"] and second["results"]["file"]["ok"]
    assert first["results"]["file"]["hash"] == second["results"]["file"]["hash"] == \
        uploaded["expect"]["artifacts"]["file"][1]
