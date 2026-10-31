import socket
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from unittest import TestCase, mock

import requests

from apiserver.bll import webhooks
from apiserver.bll.task import utils as task_utils
from apiserver.bll.task.utils import ChangeStatusRequest
from apiserver.database.model.task.task import Task, TaskStatus
from apiserver.idp import repo_visibility
from apiserver.idp.repo_visibility import ProbeBlocked

PUBLIC = "93.184.216.34"  # a public address (example.com), used with the resolver mocked
URL = "https://hooks.example.org/services/abc"


def resolving(*addresses):
    """A resolver mock answering with the given addresses, in getaddrinfo's shape."""
    infos = []
    for address in addresses:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sockaddr = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
        infos.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
    return mock.patch.object(repo_visibility.socket, "getaddrinfo", return_value=infos)


def project_query(project):
    """The query Project.objects(...).only(...).first() that deliver() makes."""
    query = mock.Mock()
    query.only.return_value.first.return_value = project
    return mock.patch.object(webhooks.Project, "objects", return_value=query)


def project_with(url):
    return SimpleNamespace(name="Demo", notification_webhook=url)


JOB = dict(
    company="c1", project="p1", task_id="t1", name="train", status=TaskStatus.failed,
    duration=3600, link="https://cairnml.example/cairnml/projects/p1/tasks/t1",
)


class TestUrlValidation(TestCase):
    def problem(self, url, insecure=()):
        with mock.patch.object(webhooks.config, "get", return_value=list(insecure)):
            return webhooks.url_problem(url)

    def test_https_urls_are_accepted(self):
        self.assertIsNone(self.problem(URL))
        self.assertIsNone(self.problem("https://hooks.example.org:8443/a/b?token=1"))

    def test_plain_http_is_refused_unless_the_host_is_a_listed_test_host(self):
        self.assertIsNotNone(self.problem("http://hooks.example.org/a"))
        self.assertIsNone(self.problem("http://127.0.0.1:9000/hook", insecure=["127.0.0.1"]))
        self.assertIsNotNone(self.problem("http://10.0.0.5/hook", insecure=["127.0.0.1"]))

    def test_other_schemes_and_hosts_are_refused(self):
        for url in ("ftp://hooks.example.org/a", "file:///etc/passwd", "https:///path", "not a url", "https://a.b/ c"):
            with self.subTest(url=url):
                self.assertIsNotNone(self.problem(url))

    def test_credentials_are_refused(self):
        self.assertIsNotNone(self.problem("https://user:pw@hooks.example.org/a"))
        self.assertIsNotNone(self.problem("https://@hooks.example.org/a"))

    def test_bad_ports_and_long_urls_are_refused(self):
        self.assertIsNotNone(self.problem("https://hooks.example.org:notaport/a"))
        self.assertIsNotNone(self.problem("https://hooks.example.org/" + "a" * webhooks.MAX_URL_LENGTH))

    def test_validate_raises_a_validation_error(self):
        with mock.patch.object(webhooks.config, "get", return_value=[]):
            with self.assertRaises(webhooks.errors.bad_request.ValidationError):
                webhooks.validate_url("http://hooks.example.org/a")
            webhooks.validate_url(URL)


class TestPayload(TestCase):
    def deliver(self, project, job=JOB):
        with project_query(project), \
                mock.patch.object(webhooks, "send", return_value=200) as send, \
                mock.patch.object(webhooks.audit, "record") as record:
            webhooks.deliver(job)
        return send, record

    def test_payload_has_the_status_fields_and_nothing_else(self):
        send, _ = self.deliver(project_with(URL))
        url, payload = send.call_args.args
        self.assertEqual(url, URL)
        self.assertEqual(set(payload), {"project", "task_id", "name", "status", "duration", "link"})
        self.assertEqual(payload["project"], "Demo")
        self.assertEqual(payload["task_id"], "t1")
        self.assertEqual(payload["name"], "train")
        self.assertEqual(payload["status"], TaskStatus.failed)
        self.assertEqual(payload["duration"], 3600)
        self.assertEqual(payload["link"], JOB["link"])

    def test_payload_has_no_parameters_or_secrets(self):
        job = dict(JOB, name="train", parameters={"lr": 0.1, "api_key": "s3cr3t"})
        send, _ = self.deliver(project_with(URL), job=job)
        payload = send.call_args.args[1]
        serialised = repr(payload)
        self.assertNotIn("s3cr3t", serialised)
        self.assertNotIn("api_key", serialised)
        self.assertNotIn("lr", payload)
        self.assertNotIn(URL, serialised)

    def test_no_delivery_without_a_webhook(self):
        with project_query(project_with(None)), mock.patch.object(webhooks, "send") as send:
            webhooks.deliver(JOB)
        send.assert_not_called()

    def test_a_delivery_writes_one_audit_event_with_project_and_status_code_only(self):
        _, record = self.deliver(project_with(URL))
        record.assert_called_once_with("system", "webhook.sent", company="c1", project="p1", status_code=200)

    def test_a_failed_delivery_is_audited_as_failed(self):
        with project_query(project_with(URL)), \
                mock.patch.object(webhooks, "send", side_effect=requests.ConnectionError), \
                mock.patch.object(webhooks.time, "sleep"), \
                mock.patch.object(webhooks.audit, "record") as record:
            webhooks.deliver(JOB)
        record.assert_called_once_with("system", "webhook.failed", company="c1", project="p1", status_code=None)

    def test_duration_is_whole_seconds_and_needs_both_times(self):
        start = datetime(2026, 10, 31, 10, 0, 0)
        end = start + timedelta(seconds=90, milliseconds=500)
        self.assertEqual(webhooks.duration_sec(start, end.replace(tzinfo=timezone.utc)), 90)
        self.assertIsNone(webhooks.duration_sec(None, end))
        self.assertIsNone(webhooks.duration_sec(start, None))


class TestAddressRules(TestCase):
    BLOCKED = [
        "127.0.0.1", "169.254.169.254", "10.0.0.4", "192.168.1.9", "172.16.0.5",
        "100.64.0.1", "fe80::1", "fd00::4", "::ffff:127.0.0.1",
    ]

    def test_a_blocked_address_is_never_contacted_and_not_retried(self):
        for address in self.BLOCKED:
            with self.subTest(address=address), resolving(address), \
                    mock.patch.object(requests.Session, "post") as post, \
                    mock.patch.object(webhooks.time, "sleep") as sleep:
                sent, code = webhooks.deliver_with_retries(URL, {})
            self.assertEqual((sent, code), (False, None))
            post.assert_not_called()
            sleep.assert_not_called()

    def test_one_blocked_address_among_public_ones_blocks_the_delivery(self):
        with resolving(PUBLIC, "10.0.0.4"), mock.patch.object(requests.Session, "post") as post:
            with self.assertRaises(ProbeBlocked):
                webhooks.send(URL, {})
        post.assert_not_called()

    def test_the_request_connects_to_the_checked_address_and_sends_the_host_name(self):
        response = mock.Mock(status_code=204)
        with resolving(PUBLIC), mock.patch.object(requests.Session, "post", return_value=response) as post:
            self.assertEqual(webhooks.send(URL, {"a": 1}), 204)
        target = post.call_args.args[0]
        self.assertEqual(target, f"https://{PUBLIC}:443/services/abc")
        self.assertEqual(post.call_args.kwargs["headers"], {"Host": "hooks.example.org"})
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertEqual(post.call_args.kwargs["json"], {"a": 1})


class TestRetries(TestCase):
    def test_a_failing_receiver_is_tried_once_and_retried_three_times(self):
        with mock.patch.object(webhooks, "send", side_effect=requests.ConnectionError) as send, \
                mock.patch.object(webhooks.time, "sleep") as sleep:
            sent, code = webhooks.deliver_with_retries(URL, {})
        self.assertEqual((sent, code), (False, None))
        self.assertEqual(send.call_count, 1 + webhooks.MAX_RETRIES)
        self.assertEqual(webhooks.MAX_RETRIES, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2, 4, 8])

    def test_a_server_error_is_retried_until_the_receiver_answers(self):
        responses = [mock.Mock(status_code=500), mock.Mock(status_code=503), mock.Mock(status_code=204)]
        with mock.patch.object(requests.Session, "post", side_effect=responses) as post, \
                resolving(PUBLIC), mock.patch.object(webhooks.time, "sleep"):
            self.assertEqual(webhooks.deliver_with_retries(URL, {}), (True, 204))
        self.assertEqual(post.call_count, 3)

    def test_a_success_is_not_retried(self):
        with mock.patch.object(webhooks, "send", return_value=200) as send, mock.patch.object(webhooks.time, "sleep"):
            self.assertEqual(webhooks.deliver_with_retries(URL, {}), (True, 200))
        send.assert_called_once()


class TestStatusChangeTrigger(TestCase):
    def change(self, new_status, current):
        task = Task(id="t1", company="c1", project="p1", name="train", status=current,
                    started=datetime.utcnow())
        with mock.patch.object(task_utils.Task, "objects") as objects, \
                mock.patch.object(task_utils, "update_project_time"), \
                mock.patch.object(webhooks, "_pool") as pool:
            objects.return_value.update.return_value = 1
            ChangeStatusRequest(task=task, new_status=new_status, force=True, user_id="u1").execute()
        return pool

    def test_completed_failed_and_stopped_queue_a_delivery(self):
        for status in (TaskStatus.completed, TaskStatus.failed, TaskStatus.stopped):
            with self.subTest(status=status):
                pool = self.change(status, TaskStatus.in_progress)
                pool.submit.assert_called_once()
                fn, job = pool.submit.call_args.args
                self.assertIs(fn, webhooks.deliver)
                self.assertEqual(job["status"], status)
                self.assertEqual(job["task_id"], "t1")
                self.assertEqual(job["company"], "c1")

    def test_other_statuses_queue_nothing(self):
        pool = self.change(TaskStatus.in_progress, TaskStatus.queued)
        pool.submit.assert_not_called()
