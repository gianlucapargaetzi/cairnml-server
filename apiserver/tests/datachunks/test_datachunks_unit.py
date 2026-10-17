"""Object storage rules that need no database or store: names, digests, keys, tags and what a presigned PUT signs.

Run inside the server image: python -m unittest apiserver.tests.datachunks.test_datachunks_unit
"""

import base64
import hashlib
from unittest import TestCase
from urllib.parse import parse_qs, urlparse

from apiserver.apierrors import errors
from apiserver.bll import datachunks as bll
from apiserver.config_repo import config

SHA = hashlib.sha256(b"payload").hexdigest()


class TestNames(TestCase):
    def test_nested_names_and_names_with_spaces_are_accepted(self):
        self.assertEqual(bll.sanitise_name("weights/model.pt"), "weights/model.pt")
        self.assertEqual(bll.sanitise_name("my plot (1).png"), "my plot (1).png")

    def test_names_that_escape_or_are_malformed_are_refused(self):
        for name in ("../x", "/abs", "a//b", "a/./b", "a/../b", "", "a\nb", "x" * 1025, "a\x00b"):
            with self.subTest(name=name), self.assertRaises(errors.bad_request.ValidationError):
                bll.sanitise_name(name)


class TestDigest(TestCase):
    def test_a_full_lowercase_hex_digest_is_accepted(self):
        self.assertEqual(bll.validate_digest(SHA), SHA)

    def test_missing_short_or_uppercase_digests_are_refused(self):
        for value in ("", SHA[:10], SHA.upper(), "z" * 64):
            with self.subTest(value=value), self.assertRaises(errors.bad_request.ValidationError):
                bll.validate_digest(value)


class TestKeys(TestCase):
    def test_key_is_project_task_and_name(self):
        self.assertEqual(bll.build_key("proj", "task1", "a/b.bin"), "proj/task1/a/b.bin")

    def test_a_task_without_project_gets_a_placeholder(self):
        self.assertEqual(bll.build_key("", "task1", "b.bin"), "_no_project/task1/b.bin")


class TestDatasetTag(TestCase):
    def test_dataset_data_needs_the_dataset_tag(self):
        with self.assertRaises(errors.forbidden.NoWritePermission):
            bll.check_kind_allowed("dataset", ["other"])

    def test_dataset_data_is_allowed_on_a_dataset_task(self):
        self.assertEqual(bll.check_kind_allowed("dataset", ["dataset"]), "dataset")

    def test_other_kinds_do_not_need_the_tag(self):
        self.assertEqual(bll.check_kind_allowed("artifact", []), "artifact")

    def test_unknown_kinds_are_refused(self):
        with self.assertRaises(errors.bad_request.ValidationError):
            bll.check_kind_allowed("video", [])


class TestPresignedPut(TestCase):
    """Signing is local, so this runs without a store. It shows what the URL is bound to."""

    def _url(self, content_type=""):
        checksum = bll._b64_digest(SHA)
        params = {"Bucket": "artifact", "Key": "p/t/x.bin", "ContentLength": 7, "ChecksumSHA256": checksum}
        if content_type:
            params["ContentType"] = content_type
        return bll._signer().generate_presigned_url("put_object", Params=params, ExpiresIn=bll._ttl())

    def test_the_url_names_the_public_host_and_expires_in_the_configured_time(self):
        url = urlparse(self._url())
        self.assertEqual(f"{url.scheme}://{url.netloc}", config.get("apiserver.storage.public_endpoint"))
        self.assertEqual(parse_qs(url.query)["X-Amz-Expires"], [str(bll._ttl())])

    def test_the_signature_covers_size_and_checksum(self):
        signed = parse_qs(urlparse(self._url()).query)["X-Amz-SignedHeaders"][0].split(";")
        self.assertIn("content-length", signed)
        self.assertIn("x-amz-checksum-sha256", signed)

    def test_the_key_and_bucket_are_in_the_path(self):
        self.assertIn("/artifact/p/t/x.bin", urlparse(self._url()).path)

    def test_the_checksum_is_the_base64_of_the_digest(self):
        self.assertEqual(bll._b64_digest(SHA), base64.b64encode(bytes.fromhex(SHA)).decode())
