"""A disabled admin may not enrol an agent, even with a valid sign-in. Run inside the server container."""

import uuid
from unittest import TestCase

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from apiserver import database as db
from apiserver.database.model.auth import User
from apiserver.idp.users import user_id_for
from apiserver.config_repo import config
from apiserver.tests.idp.test_devices_api import call, idp_token

ISSUER = config.get("apiserver.idp.issuer")


def _public_pem(key):
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


class TestDisabledAdminCannotEnrolAnAgent(TestCase):
    @classmethod
    def setUpClass(cls):
        db.db.initialize()

    def setUp(self):
        self.sub = f"admin-{uuid.uuid4().hex}"
        self.user_id = user_id_for(ISSUER, self.sub)
        self.addCleanup(lambda: User.objects(id=self.user_id).delete())

    def test_a_disabled_admin_is_refused_for_an_agent(self):
        first = call("devices.enroll", {"kind": "client", "public_key": _public_pem(Ed25519PrivateKey.generate())},
                     token=idp_token(sub=self.sub, roles=("admin",)))
        self.assertEqual(first.status_code, 200, first.text)
        User.objects(id=self.user_id).update_one(set__idp_disabled=True)

        response = call("devices.enroll", {"kind": "agent", "public_key": _public_pem(Ed25519PrivateKey.generate())},
                        token=idp_token(sub=self.sub, roles=("admin",)))
        self.assertNotEqual(response.status_code, 200, response.text)
