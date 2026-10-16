from unittest import TestCase

from mongoengine import ValidationError

from apiserver.database.model.auth import User
from apiserver.database.model.device import Device


class TestDeviceModel(TestCase):
    def _device(self, **kwargs):
        base = dict(id="dev_1", user="u1", company="c1", kind="client", name="laptop", public_key="pem")
        base.update(kwargs)
        return Device(**base)

    def test_valid_client_device_validates(self):
        self._device().validate()

    def test_agent_kind_validates(self):
        self._device(kind="agent").validate()

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ValidationError):
            self._device(kind="robot").validate()

    def test_device_is_not_revoked_by_default(self):
        self.assertFalse(self._device().revoked)


class TestIdpUserFields(TestCase):
    def test_user_has_idp_fields(self):
        fields = User._fields
        self.assertIn("idp_subject", fields)
        self.assertIn("idp_disabled", fields)
        self.assertTrue(fields["idp_subject"].unique)
        self.assertTrue(fields["idp_subject"].sparse)
        self.assertFalse(User(id="u", company="c").idp_disabled)
