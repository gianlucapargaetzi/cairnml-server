"""The keys handed out for the dataset bucket: what the session policy allows, and what the response carries.

Run inside the server image: python -m unittest apiserver.tests.datachunks.test_dataset_credentials_unit
"""

import json
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import MagicMock, patch

from apiserver.bll import dataset_credentials as bll


def _allowed(policy):
    return {(action, resource) for s in policy["Statement"] for action in s["Action"] for resource in s["Resource"]}


class TestSessionPolicy(TestCase):
    def test_only_the_dataset_bucket_is_reachable(self):
        resources = {r for s in bll.SESSION_POLICY["Statement"] for r in s["Resource"]}
        self.assertEqual(resources, {"arn:aws:s3:::dataset/*", "arn:aws:s3:::dataset"})

    def test_objects_can_be_read_and_written_but_not_deleted(self):
        allowed = _allowed(bll.SESSION_POLICY)
        self.assertIn(("s3:GetObject", "arn:aws:s3:::dataset/*"), allowed)
        self.assertIn(("s3:PutObject", "arn:aws:s3:::dataset/*"), allowed)
        self.assertNotIn(("s3:DeleteObject", "arn:aws:s3:::dataset/*"), allowed)

    def test_the_bucket_can_be_listed(self):
        self.assertIn(("s3:ListBucket", "arn:aws:s3:::dataset"), _allowed(bll.SESSION_POLICY))

    def test_statements_do_not_mix_action_families(self):
        for statement in bll.SESSION_POLICY["Statement"]:
            self.assertEqual({a.split(":")[0] for a in statement["Action"]}, {"s3"})


class TestCredentials(TestCase):
    def _issue(self):
        sts = MagicMock()
        sts.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "AK",
                "SecretAccessKey": "SK",
                "SessionToken": "ST",
                "Expiration": datetime(2026, 10, 23, 10, 36, 24, tzinfo=timezone.utc),
            }
        }
        with patch.object(bll, "_sts", return_value=sts), patch.object(bll, "_ttl", return_value=1800):
            return bll.dataset_credentials(), sts

    def test_the_session_is_limited_by_the_policy_and_the_ttl(self):
        _, sts = self._issue()
        kwargs = sts.assume_role.call_args.kwargs
        self.assertEqual(json.loads(kwargs["Policy"]), bll.SESSION_POLICY)
        self.assertEqual(kwargs["DurationSeconds"], 1800)

    def test_the_response_names_the_bucket_and_the_expiry(self):
        result, _ = self._issue()
        self.assertEqual(result["bucket"], "dataset")
        self.assertEqual(result["access_key"], "AK")
        self.assertEqual(result["secret_key"], "SK")
        self.assertEqual(result["session_token"], "ST")
        self.assertTrue(result["expires_at"].startswith("2026-10-23T10:36:24"))
