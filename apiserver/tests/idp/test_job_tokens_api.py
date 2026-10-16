"""Job tokens over HTTP against the live server and the real database. Run inside the server container."""

import os
import uuid
from unittest import TestCase

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from apiserver import database as db
from apiserver.database.model.task.task import Task
from apiserver.idp.assertion import make_assertion
from apiserver.tests.idp.test_devices_api import API, PUBLIC_URL, call, idp_token


def _public_pem(key):
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def _agent(sub):
    """Enrol an agent as an admin and return (device id, agent access token)."""
    key = Ed25519PrivateKey.generate()
    enrolled = call("devices.enroll", {"kind": "agent", "public_key": _public_pem(key)},
                    token=idp_token(sub=sub, roles=("admin",)))
    device_id = enrolled.json()["data"]["id"]
    assertion = make_assertion(key, device_id, PUBLIC_URL)
    token = call("devices.exchange", {"assertion": assertion}).json()["data"]["access_token"]
    return device_id, token


def _new_task():
    from apiserver.tests.idp.bearer import suite_bearer

    response = requests.post(f"{API}/tasks.create", json={"name": f"job-{uuid.uuid4().hex[:8]}", "type": "training"},
                             headers={"Authorization": f"Bearer {suite_bearer()}"}, timeout=20)
    return response.json()["data"]["id"]


class TestJobTokens(TestCase):
    @classmethod
    def setUpClass(cls):
        db.db.initialize()

    def setUp(self):
        self.device_id, self.agent_token = _agent(f"agent-{uuid.uuid4().hex}")
        self.task_id = _new_task()
        self.addCleanup(lambda: Task.objects(id=self.task_id).delete())
        Task.objects(id=self.task_id).update_one(set__status="queued", set__last_worker=self.device_id)

    def job_token(self, task_id=None, token=None):
        return call("devices.job_token", {"task": task_id or self.task_id}, token=token or self.agent_token)

    def test_the_agent_running_its_task_gets_a_job_token(self):
        response = self.job_token()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["data"]["access_token"])

    def test_the_job_token_is_refused_once_the_task_stops(self):
        token = self.job_token().json()["data"]["access_token"]
        self.assertEqual(call("devices.get_all", token=token).status_code, 200)
        Task.objects(id=self.task_id).update_one(set__status="stopped")
        self.assertEqual(call("devices.get_all", token=token).status_code, 401)

    def test_an_agent_cannot_get_a_token_for_another_agents_task(self):
        other_device, other_token = _agent(f"agent-{uuid.uuid4().hex}")
        self.assertEqual(self.job_token(token=other_token).status_code, 401)

    def test_a_task_that_has_not_started_is_refused(self):
        Task.objects(id=self.task_id).update_one(set__status="created")
        self.assertEqual(self.job_token().status_code, 401)

    def test_a_laptop_cannot_get_a_job_token(self):
        key = Ed25519PrivateKey.generate()
        device = call("devices.enroll", {"kind": "client", "public_key": _public_pem(key)},
                      token=idp_token(sub=f"laptop-{uuid.uuid4().hex}")).json()["data"]["id"]
        token = call("devices.exchange", {"assertion": make_assertion(key, device, PUBLIC_URL)}).json()["data"]["access_token"]
        Task.objects(id=self.task_id).update_one(set__last_worker=device)
        self.assertEqual(self.job_token(token=token).status_code, 401)


class TestGitToken(TestCase):
    @classmethod
    def setUpClass(cls):
        db.db.initialize()

    def setUp(self):
        self.device_id, self.agent_token = _agent(f"agent-{uuid.uuid4().hex}")
        self.task_id = _new_task()
        self.addCleanup(lambda: Task.objects(id=self.task_id).delete())
        Task.objects(id=self.task_id).update_one(set__status="queued", set__last_worker=self.device_id)

    def test_without_a_configured_app_no_repository_token_is_given(self):
        response = call("devices.git_token", {"task": self.task_id}, token=self.agent_token)
        self.assertEqual(response.status_code, 400)
        self.assertIn("GitHub is not configured", response.text)

    def test_a_job_token_for_another_task_is_refused(self):
        token = call("devices.job_token", {"task": self.task_id}, token=self.agent_token).json()["data"]["access_token"]
        other = _new_task()
        self.addCleanup(lambda: Task.objects(id=other).delete())
        response = call("devices.git_token", {"task": other}, token=token)
        self.assertEqual(response.status_code, 401)
