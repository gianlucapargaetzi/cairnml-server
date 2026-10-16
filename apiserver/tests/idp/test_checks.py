from types import SimpleNamespace
from unittest import TestCase

from apiserver.idp.checks import AccessDenied, check_device, check_job


def owner(disabled=False):
    return SimpleNamespace(id="u1", idp_disabled=disabled)


def device(revoked=False):
    return SimpleNamespace(id="dev_1", user="u1", revoked=revoked)


class TestDeviceChecks(TestCase):
    def test_live_device_of_an_enabled_user_is_allowed(self):
        check_device(device(), owner())

    def test_revoked_device_is_refused(self):
        with self.assertRaises(AccessDenied):
            check_device(device(revoked=True), owner())

    def test_disabled_owner_is_refused(self):
        with self.assertRaises(AccessDenied):
            check_device(device(), owner(disabled=True))

    def test_missing_device_is_refused(self):
        with self.assertRaises(AccessDenied):
            check_device(None, owner())

    def test_missing_owner_is_refused(self):
        with self.assertRaises(AccessDenied):
            check_device(device(), None)


class TestJobChecks(TestCase):
    def test_queued_and_running_jobs_are_allowed(self):
        check_job("queued", owner())
        check_job("in_progress", owner())

    def test_finished_or_stopped_jobs_are_refused(self):
        for status in ("created", "stopped", "failed", "completed", "closed", "published", "publishing", "unknown"):
            with self.assertRaises(AccessDenied, msg=status):
                check_job(status, owner())

    def test_job_of_a_disabled_owner_is_refused(self):
        with self.assertRaises(AccessDenied):
            check_job("in_progress", owner(disabled=True))

    def test_missing_task_is_refused(self):
        with self.assertRaises(AccessDenied):
            check_job(None, owner())


class TestWebSessionChecks(TestCase):
    def test_enabled_user_may_use_a_web_session(self):
        from apiserver.idp.checks import check_user
        check_user(owner())

    def test_disabled_user_may_not_use_a_web_session(self):
        from apiserver.idp.checks import check_user
        with self.assertRaises(AccessDenied):
            check_user(owner(disabled=True))


class TestJobIssueChecks(TestCase):
    def agent(self, kind="agent", device_id="dev_a"):
        return SimpleNamespace(id=device_id, kind=kind, revoked=False)

    def test_agent_running_its_own_queued_task_may_get_a_job_token(self):
        from apiserver.idp.checks import check_job_issue
        check_job_issue("queued", "dev_a", self.agent())
        check_job_issue("in_progress", "dev_a", self.agent())

    def test_a_client_device_may_not_get_a_job_token(self):
        from apiserver.idp.checks import check_job_issue
        with self.assertRaises(AccessDenied):
            check_job_issue("queued", "dev_a", self.agent(kind="client"))

    def test_another_agents_task_is_refused(self):
        from apiserver.idp.checks import check_job_issue
        with self.assertRaises(AccessDenied):
            check_job_issue("queued", "dev_b", self.agent())

    def test_a_finished_task_is_refused(self):
        from apiserver.idp.checks import check_job_issue
        with self.assertRaises(AccessDenied):
            check_job_issue("stopped", "dev_a", self.agent())

    def test_a_missing_device_is_refused(self):
        from apiserver.idp.checks import check_job_issue
        with self.assertRaises(AccessDenied):
            check_job_issue("queued", "dev_a", None)
