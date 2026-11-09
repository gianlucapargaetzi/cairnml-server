"""A repository no access reaches is refused as AccessRefused, which the enqueue turns into a Failed task. Other
validation errors keep refusing the submission.

Run inside the server image: python -m unittest apiserver.tests.datachunks.test_access_refused_unit
"""

from unittest import TestCase

from apiserver.apierrors import errors
from apiserver.idp.access_refused import AccessRefused


class TestAccessRefused(TestCase):
    def test_it_is_a_validation_error_so_other_callers_still_refuse(self):
        self.assertTrue(issubclass(AccessRefused, errors.bad_request.ValidationError))

    def test_a_plain_validation_error_is_not_an_access_refusal(self):
        self.assertFalse(issubclass(errors.bad_request.ValidationError, AccessRefused))

