"""Object storage endpoints against the running server and the real store. Run inside the server container.

Needs the test identity-provider key set (make_test_idp.py), a server with jwks_file set to it, and RustFS
reachable from the server at the public endpoint. See the README of the deployment for the test stack.
"""

import base64
import hashlib
import os
import urllib.request
import uuid
from unittest import TestCase

import requests
from botocore.exceptions import ClientError
from unittest import SkipTest

from apiserver import database as db
from apiserver.bll import datachunks as bll
from apiserver.database.model.datachunk import Datachunk
from apiserver.database.model.task.task import Task
from apiserver.tests.idp.bearer import suite_bearer
from apiserver.config_repo import config
from apiserver.tests.idp.test_devices_api import API, SERVER, call, idp_token
from apiserver.tests.idp.test_job_tokens_api import _agent, _new_task


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _post(endpoint, body, token):
    return requests.post(f"{API}/{endpoint}", json=body, headers=_bearer(token), timeout=20)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


class TestDatachunksApi(TestCase):
    @classmethod
    def setUpClass(cls):
        db.db.initialize()

    def setUp(self):
        self.user = suite_bearer()
        self.task_id = _new_task()
        self.addCleanup(lambda: Task.objects(id=self.task_id).delete())
        self.body = b"artifact bytes " + uuid.uuid4().bytes
        self.sha = _sha(self.body)

    def upload_request(self, **overrides):
        request = {"task": self.task_id, "kind": "artifact", "name": f"w/{uuid.uuid4().hex}.bin",
                   "size": len(self.body), "sha256": self.sha, "content_type": "application/octet-stream"}
        request.update(overrides)
        return request

    def put(self, grant, data=None):
        headers = dict(grant["headers"])
        req = urllib.request.Request(grant["url"], data=self.body if data is None else data,
                                     method="PUT", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    def test_client_uploads_commits_and_downloads(self):
        grant = _post("datachunks.prepare_upload", self.upload_request(), self.user).json()["data"]
        self.assertEqual(grant["method"], "PUT")
        self.assertEqual(self.put(grant), 200)

        committed = _post("datachunks.commit", {"id": grant["id"]}, self.user)
        self.assertEqual(committed.status_code, 200, committed.text)
        self.assertEqual(committed.json()["data"]["state"], "stored")

        download = _post("datachunks.prepare_download", {"id": grant["id"]}, self.user).json()["data"]
        self.assertEqual(urllib.request.urlopen(download["url"], timeout=20).read(), self.body)

    def test_the_key_is_built_by_the_server(self):
        grant = _post("datachunks.prepare_upload", self.upload_request(name="a.bin"), self.user).json()["data"]
        chunk = Datachunk.objects(id=grant["id"]).first()
        self.addCleanup(chunk.delete)
        self.assertTrue(chunk.key.endswith(f"/{self.task_id}/a.bin"), chunk.key)
        self.assertEqual(chunk.bucket, "artifact")

    def test_commit_before_the_bytes_are_there_is_refused(self):
        grant = _post("datachunks.prepare_upload", self.upload_request(), self.user).json()["data"]
        response = _post("datachunks.commit", {"id": grant["id"]}, self.user)
        self.assertEqual(response.status_code, 400, response.text)

    def test_bytes_that_do_not_match_the_signed_checksum_are_refused_by_the_store(self):
        grant = _post("datachunks.prepare_upload", self.upload_request(), self.user).json()["data"]
        # The store refuses the bytes: 400 BadDigest when the checksum header disagrees with the body,
        # 403 when the signature does not match. Either way nothing is stored.
        self.assertIn(self.put(grant, data=b"x" * len(self.body)), (400, 403))
        response = _post("datachunks.commit", {"id": grant["id"]}, self.user)
        self.assertEqual(response.status_code, 400, response.text)

    def test_a_bad_digest_size_or_name_is_refused(self):
        for overrides in ({"sha256": "abc"}, {"size": -1}, {"name": "../escape"}, {"kind": "video"}):
            with self.subTest(overrides=overrides):
                response = _post("datachunks.prepare_upload", self.upload_request(**overrides), self.user)
                self.assertEqual(response.status_code, 400, response.text)

    def test_a_dataset_needs_a_dataset_task(self):
        response = _post("datachunks.prepare_upload", self.upload_request(kind="dataset"), self.user)
        self.assertEqual(response.status_code, 403, response.text)

    def test_no_token_is_refused(self):
        response = requests.post(f"{API}/datachunks.prepare_upload", json=self.upload_request(), timeout=20)
        self.assertEqual(response.status_code, 401)

    def test_download_by_task_kind_and_name_returns_the_newest_stored_object(self):
        name = f"metrics/{uuid.uuid4().hex}.png"
        first = self._store(name=name)
        second = self._store(name=name, body=b"second version " + uuid.uuid4().bytes)
        response = _post("datachunks.prepare_download",
                         {"task": self.task_id, "kind": "artifact", "name": name}, self.user)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["id"], second)
        self.assertNotEqual(first, second)

    def test_names_with_spaces_round_trip(self):
        self._store(name=f"my plot {uuid.uuid4().hex} (1).png")

    def test_delete_by_name_removes_the_object_and_the_record(self):
        name = f"gone/{uuid.uuid4().hex}.bin"
        chunk_id = self._store(name=name)
        chunk = Datachunk.objects(id=chunk_id).first()
        response = _post("datachunks.delete", {"task": self.task_id, "kind": "artifact", "name": name}, self.user)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["deleted"], 1)
        self.assertFalse(Datachunk.objects(id=chunk_id).first())
        with self.assertRaises(ClientError):
            bll._internal().head_object(Bucket=chunk.bucket, Key=chunk.key)
        gone = _post("datachunks.prepare_download", {"task": self.task_id, "kind": "artifact", "name": name}, self.user)
        self.assertEqual(gone.status_code, 400, gone.text)

    def test_delete_without_an_identifier_is_refused(self):
        response = _post("datachunks.delete", {"task": self.task_id}, self.user)
        self.assertEqual(response.status_code, 400, response.text)

    def _store(self, name, body=None):
        body = self.body if body is None else body
        grant = _post("datachunks.prepare_upload",
                      self.upload_request(name=name, size=len(body), sha256=_sha(body)), self.user).json()["data"]
        self.assertEqual(self.put(dict(grant, headers=grant["headers"]), data=body), 200)
        self.assertEqual(_post("datachunks.commit", {"id": grant["id"]}, self.user).status_code, 200)
        self.addCleanup(lambda: Datachunk.objects(id=grant["id"]).delete())
        return grant["id"]

    def test_browser_link_redirects_to_the_presigned_url_for_a_bearer_token(self):
        name = f"browser/{uuid.uuid4().hex} x.bin"
        self._store(name=name)
        url = f"{SERVER}/datachunks/artifact/{self.task_id}/{name}"
        response = requests.get(url, headers=_bearer(self.user), allow_redirects=False, timeout=20)
        self.assertEqual(response.status_code, 302, response.text)
        self.assertEqual(urllib.request.urlopen(response.headers["Location"], timeout=20).read(), self.body)

    def test_browser_link_accepts_the_session_cookie(self):
        name = f"cookie/{uuid.uuid4().hex}.bin"
        self._store(name=name)
        cookie = {config.get("apiserver.auth.session_auth_cookie_name"): self.user}
        url = f"{SERVER}/datachunks/artifact/{self.task_id}/{name}"
        response = requests.get(url, cookies=cookie, allow_redirects=False, timeout=20)
        self.assertEqual(response.status_code, 302, response.text)

    def test_browser_link_without_credentials_is_refused(self):
        url = f"{SERVER}/datachunks/artifact/{self.task_id}/anything.bin"
        self.assertEqual(requests.get(url, allow_redirects=False, timeout=20).status_code, 401)

    def test_browser_link_to_a_missing_object_is_not_found(self):
        url = f"{SERVER}/datachunks/artifact/{self.task_id}/never-uploaded.bin"
        response = requests.get(url, headers=_bearer(self.user), allow_redirects=False, timeout=20)
        self.assertEqual(response.status_code, 404)

    def test_download_before_commit_is_refused(self):
        grant = _post("datachunks.prepare_upload", self.upload_request(), self.user).json()["data"]
        response = _post("datachunks.prepare_download", {"id": grant["id"]}, self.user)
        self.assertEqual(response.status_code, 400, response.text)


class TestJobTokensForStorage(TestCase):
    @classmethod
    def setUpClass(cls):
        db.db.initialize()

    def setUp(self):
        self.device_id, self.agent_token = _agent(f"agent-{uuid.uuid4().hex}")
        self.task_id = _new_task()
        self.other_task = _new_task()
        self.addCleanup(lambda: Task.objects(id__in=[self.task_id, self.other_task]).delete())
        Task.objects(id=self.task_id).update_one(set__status="in_progress", set__last_device=self.device_id)
        self.job = call("devices.job_token", {"task": self.task_id}, token=self.agent_token).json()["data"]["access_token"]
        self.body = b"job output " + uuid.uuid4().bytes

    def request(self, task, **kw):
        body = {"task": task, "kind": "artifact", "name": f"out/{uuid.uuid4().hex}.bin",
                "size": len(self.body), "sha256": _sha(self.body)}
        body.update(kw)
        return body

    def test_a_job_uploads_for_its_own_task(self):
        response = _post("datachunks.prepare_upload", self.request(self.task_id), self.job)
        self.assertEqual(response.status_code, 200, response.text)

    def test_a_job_cannot_upload_for_another_task(self):
        response = _post("datachunks.prepare_upload", self.request(self.other_task), self.job)
        self.assertIn(response.status_code, (401, 403), response.text)

    def _stored_in(self, task_id):
        """An object stored for a task by a person with write access; returns its name."""
        body = b"stored for " + task_id.encode() + uuid.uuid4().bytes
        name = f"reads/{uuid.uuid4().hex}.bin"
        user = suite_bearer()
        grant = _post("datachunks.prepare_upload",
                      {"task": task_id, "kind": "artifact", "name": name, "size": len(body), "sha256": _sha(body)},
                      user).json()["data"]
        self.assertEqual(requests.put(grant["url"], data=body, headers=grant["headers"], timeout=20).status_code, 200)
        self.assertEqual(_post("datachunks.commit", {"id": grant["id"]}, user).status_code, 200)
        self.addCleanup(lambda: Datachunk.objects(id=grant["id"]).delete())
        return name

    def test_a_job_downloads_its_own_tasks_data(self):
        name = self._stored_in(self.task_id)
        url = f"{SERVER}/datachunks/artifact/{self.task_id}/{name}"
        response = requests.get(url, headers=_bearer(self.job), allow_redirects=False, timeout=20)
        self.assertEqual(response.status_code, 302, response.text)
        response = _post("datachunks.prepare_download", {"task": self.task_id, "kind": "artifact", "name": name},
                         self.job)
        self.assertEqual(response.status_code, 200, response.text)

    def test_a_job_cannot_download_another_tasks_data(self):
        name = self._stored_in(self.other_task)
        url = f"{SERVER}/datachunks/artifact/{self.other_task}/{name}"
        response = requests.get(url, headers=_bearer(self.job), allow_redirects=False, timeout=20)
        self.assertEqual(response.status_code, 401, response.text)
        response = _post("datachunks.prepare_download", {"task": self.other_task, "kind": "artifact", "name": name},
                         self.job)
        self.assertEqual(response.status_code, 401, response.text)

    def test_a_person_downloads_data_of_a_task_in_their_company(self):
        name = self._stored_in(self.other_task)
        url = f"{SERVER}/datachunks/artifact/{self.other_task}/{name}"
        response = requests.get(url, headers=_bearer(suite_bearer()), allow_redirects=False, timeout=20)
        self.assertEqual(response.status_code, 302, response.text)

    def test_a_download_for_a_task_that_does_not_exist_is_refused(self):
        response = _post("datachunks.prepare_download",
                         {"task": uuid.uuid4().hex, "kind": "artifact", "name": "x.bin"}, suite_bearer())
        self.assertIn(response.status_code, (400, 404), response.text)

    def test_a_job_token_is_refused_once_its_task_stops(self):
        grant = _post("datachunks.prepare_upload", self.request(self.task_id), self.job).json()["data"]
        Task.objects(id=self.task_id).update_one(set__status="completed")
        response = _post("datachunks.commit", {"id": grant["id"]}, self.job)
        self.assertEqual(response.status_code, 401, response.text)


class TestTaskDeletion(TestCase):
    """Deleting a task removes the objects behind its artifacts. The async delete job does the work."""

    @classmethod
    def setUpClass(cls):
        db.db.initialize()

    def test_deleting_a_task_removes_its_stored_objects(self):
        import time

        user = suite_bearer()
        task_id = _new_task()
        body = b"to be deleted " + uuid.uuid4().bytes
        name = f"gone/{uuid.uuid4().hex}.bin"
        grant = _post("datachunks.prepare_upload",
                      {"task": task_id, "kind": "artifact", "name": name, "size": len(body), "sha256": _sha(body)},
                      user).json()["data"]
        self.assertEqual(requests.put(grant["url"], data=body, headers=grant["headers"], timeout=20).status_code, 200)
        self.assertEqual(_post("datachunks.commit", {"id": grant["id"]}, user).status_code, 200)
        location = f"datachunk://artifact/{task_id}/{name}"
        registered = requests.post(
            f"{SERVER}/v2.10/tasks.add_or_update_artifacts",
            json={"task": task_id, "artifacts": [{"key": "report", "type": "custom", "uri": location,
                                                  "content_size": len(body), "hash": _sha(body)}]},
            headers=_bearer(user), timeout=20)
        self.assertEqual(registered.status_code, 200, registered.text)

        chunk = Datachunk.objects(id=grant["id"]).first()
        bucket, key = chunk.bucket, chunk.key

        self.assertEqual(_post("tasks.delete", {"task": task_id}, user).status_code, 200)

        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and Datachunk.objects(id=grant["id"]).first():
            time.sleep(2)
        self.assertIsNone(Datachunk.objects(id=grant["id"]).first(), "the async delete job did not run")
        with self.assertRaises(ClientError):
            bll._internal().head_object(Bucket=bucket, Key=key)


class TestPendingUploads(TestCase):
    @classmethod
    def setUpClass(cls):
        db.db.initialize()

    def test_an_upload_that_was_never_committed_is_purged(self):
        from datetime import timedelta

        task_id = _new_task()
        self.addCleanup(lambda: Task.objects(id=task_id).delete())
        grant = _post("datachunks.prepare_upload",
                      {"task": task_id, "kind": "artifact", "name": f"never/{uuid.uuid4().hex}.bin",
                       "size": 3, "sha256": _sha(b"abc")}, suite_bearer()).json()["data"]
        self.assertIsNotNone(Datachunk.objects(id=grant["id"]).first())
        bll.purge_stale_pending(timedelta(seconds=0))
        self.assertIsNone(Datachunk.objects(id=grant["id"]).first())


class TestMultipartUploads(TestCase):
    """Objects at or above the multipart threshold. The test runs when the threshold is small enough to test
    cheaply: start the server with CLEARML__apiserver__storage__multipart_threshold_bytes=16777216 and
    CLEARML__apiserver__storage__multipart_part_bytes=5242880. Otherwise it is skipped."""

    @classmethod
    def setUpClass(cls):
        db.db.initialize()
        threshold = int(config.get("apiserver.storage.multipart_threshold_bytes"))
        if threshold > 64 * 1024 * 1024:
            raise SkipTest(f"multipart threshold is {threshold} bytes; lower it to test multipart uploads")
        cls.threshold = threshold
        cls.part_bytes = int(config.get("apiserver.storage.multipart_part_bytes"))

    def setUp(self):
        self.user = suite_bearer()
        self.task_id = _new_task()
        self.addCleanup(lambda: Task.objects(id=self.task_id).delete())
        self.body = bytes(range(256)) * ((self.threshold + 1024 * 1024) // 256)
        self.sha = _sha(self.body)

    def _start(self, **overrides):
        request = {"task": self.task_id, "kind": "artifact", "name": f"big/{uuid.uuid4().hex}.bin",
                   "size": len(self.body), "sha256": self.sha}
        request.update(overrides)
        response = _post("datachunks.prepare_upload", request, self.user)
        self.assertEqual(response.status_code, 200, response.text)
        grant = response.json()["data"]
        self.addCleanup(lambda: Datachunk.objects(id=grant["id"]).delete())
        return grant

    def _send_parts(self, grant, skip=()):
        parts = []
        count = grant["multipart"]["parts"]
        for number in range(1, count + 1):
            if number in skip:
                continue
            part = _post("datachunks.prepare_part", {"id": grant["id"], "part": number}, self.user).json()["data"]
            start = (number - 1) * grant["multipart"]["part_bytes"]
            block = self.body[start:start + grant["multipart"]["part_bytes"]]
            response = requests.put(part["url"], data=block, headers=part["headers"], timeout=60)
            self.assertEqual(response.status_code, 200, response.text)
            parts.append({"number": number, "etag": response.headers["ETag"]})
        return parts

    def test_a_large_object_is_uploaded_in_parts_and_reads_back_intact(self):
        grant = self._start()
        self.assertIn("multipart", grant)
        self.assertEqual(grant["multipart"]["parts"], -(-len(self.body) // grant["multipart"]["part_bytes"]))
        parts = self._send_parts(grant)
        committed = _post("datachunks.commit", {"id": grant["id"], "parts": parts}, self.user)
        self.assertEqual(committed.status_code, 200, committed.text)
        download = _post("datachunks.prepare_download", {"id": grant["id"]}, self.user).json()["data"]
        self.assertEqual(urllib.request.urlopen(download["url"], timeout=60).read(), self.body)

    def test_a_missing_part_is_refused(self):
        grant = self._start()
        parts = self._send_parts(grant, skip=(2,))
        response = _post("datachunks.commit", {"id": grant["id"], "parts": parts}, self.user)
        self.assertEqual(response.status_code, 400, response.text)

    def test_a_large_object_with_the_wrong_checksum_is_removed(self):
        grant = self._start(sha256=_sha(b"something else"))
        parts = self._send_parts(grant)
        response = _post("datachunks.commit", {"id": grant["id"], "parts": parts}, self.user)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIsNone(Datachunk.objects(id=grant["id"]).first())

    def test_a_part_number_outside_the_upload_is_refused(self):
        grant = self._start()
        response = _post("datachunks.prepare_part", {"id": grant["id"], "part": grant["multipart"]["parts"] + 1},
                         self.user)
        self.assertEqual(response.status_code, 400, response.text)
