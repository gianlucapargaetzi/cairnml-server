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

from apiserver import database as db
from apiserver.database.model.datachunk import Datachunk
from apiserver.database.model.task.task import Task
from apiserver.tests.idp.bearer import suite_bearer
from apiserver.tests.idp.test_devices_api import API, call, idp_token
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

    def test_a_job_token_is_refused_once_its_task_stops(self):
        grant = _post("datachunks.prepare_upload", self.request(self.task_id), self.job).json()["data"]
        Task.objects(id=self.task_id).update_one(set__status="completed")
        response = _post("datachunks.commit", {"id": grant["id"]}, self.job)
        self.assertEqual(response.status_code, 401, response.text)
