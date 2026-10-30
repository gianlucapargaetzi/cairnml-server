import uuid
from unittest import TestCase

import pytest

from apiserver.idp.replay import claim_jti


@pytest.mark.needs_redis
class TestReplayStore(TestCase):
    def test_a_jti_can_be_claimed_once(self):
        jti = uuid.uuid4().hex
        self.assertTrue(claim_jti(jti, ttl=60))
        self.assertFalse(claim_jti(jti, ttl=60))

    def test_different_jtis_are_independent(self):
        self.assertTrue(claim_jti(uuid.uuid4().hex, ttl=60))
        self.assertTrue(claim_jti(uuid.uuid4().hex, ttl=60))
