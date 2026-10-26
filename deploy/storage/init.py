"""Creates the buckets and the restricted service user the API server uses, on RustFS.

Standard library only, so the init container needs no pip install. Requests are signed with
AWS Signature V4; the admin endpoints are the RustFS ones under /rustfs/admin/v3/.
Re-running is safe: every step is idempotent. Any failure exits non-zero.

The service user gets object read, write and delete on the four buckets, the listing and multipart actions on the dataset bucket, and STS AssumeRole for the dataset keys. Nothing else. The root
user is used here only, never by the server.
"""

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from urllib.parse import quote

ENDPOINT = os.environ.get("CAIRNML_STORAGE_ENDPOINT", "http://rustfs:9000")
REGION = "us-east-1"
ROOT_USER = os.environ["RUSTFS_ROOT_USER"]
ROOT_PASSWORD = os.environ["RUSTFS_ROOT_PASSWORD"]
SERVICE_USER = os.environ.get("CAIRNML_S3_ACCESS_KEY", "cairnml-server")
with open(os.environ["CAIRNML_S3_SECRET_KEY_FILE"], encoding="utf-8") as handle:
    SERVICE_SECRET = handle.read().strip()
BUCKETS = ("artifact", "dataset", "model", "debug")
POLICY_NAME = "cairnml-server"
POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
            "Resource": [f"arn:aws:s3:::{b}/*" for b in BUCKETS],
        },
        # The server hands out short-lived keys for the dataset bucket (DVC uses them), by assuming a
        # session policy that is a subset of this one. A session can only do what this user may do,
        # so the actions DVC needs on that bucket are listed here. RustFS refuses mixed action
        # families in one statement, and a resource of "*" for sts, so each has its own.
        {
            "Effect": "Allow",
            "Action": ["s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
            "Resource": ["arn:aws:s3:::dataset/*"],
        },
        {
            "Effect": "Allow",
            "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
            "Resource": ["arn:aws:s3:::dataset"],
        },
        {"Effect": "Allow", "Action": ["sts:AssumeRole"], "Resource": ["arn:aws:s3:::*"]},
    ],
}


def _hmac(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _quote(value):
    return quote(value, safe="-_.~")


def signed_request(method, path, query=None, body=b""):
    """Sends one SigV4-signed request as the root user and returns (status, body)."""
    query = query or {}
    host = ENDPOINT.split("://", 1)[1]
    canonical_query = "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(query.items()))
    payload_hash = hashlib.sha256(body).hexdigest()
    now = datetime.now(UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
    signed = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    canonical_request = "\n".join(
        [method, quote(path, safe="/"), canonical_query, canonical_headers, signed, payload_hash]
    )
    scope = f"{date}/{REGION}/s3/aws4_request"
    canonical_hash = hashlib.sha256(canonical_request.encode()).hexdigest()
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, canonical_hash])
    key = _hmac(("AWS4" + ROOT_PASSWORD).encode(), date)
    for part in (REGION, "s3", "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    auth = (
        f"AWS4-HMAC-SHA256 Credential={ROOT_USER}/{scope}, "
        f"SignedHeaders={signed}, Signature={signature}"
    )
    url = f"{ENDPOINT}{quote(path, safe='/')}" + (f"?{canonical_query}" if canonical_query else "")
    request = urllib.request.Request(
        url,
        data=body or None,
        method=method,
        headers={**{k: v for k, v in headers.items() if k != "host"}, "Authorization": auth},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def wait_until_healthy(timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{ENDPOINT}/health", timeout=5) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(2)
    raise SystemExit(f"RustFS did not become healthy at {ENDPOINT} within {timeout}s")


def expect_ok(what, result):
    status, text = result
    if not 200 <= status < 300:
        raise SystemExit(f"{what} failed: HTTP {status} {text[:300]}")
    print(f"{what}: ok")


def main():
    wait_until_healthy()
    for bucket in BUCKETS:
        status, text = signed_request("PUT", f"/{bucket}")
        # 409 means the bucket exists already, the normal re-run path.
        if status == 409:
            print(f"bucket {bucket}: exists")
        else:
            expect_ok(f"bucket {bucket}", (status, text))
    expect_ok(
        "policy",
        signed_request(
            "PUT",
            "/rustfs/admin/v3/add-canned-policy",
            {"name": POLICY_NAME},
            json.dumps(POLICY).encode(),
        ),
    )
    expect_ok(
        "service user",
        signed_request(
            "PUT",
            "/rustfs/admin/v3/add-user",
            {"accessKey": SERVICE_USER},
            json.dumps({"secretKey": SERVICE_SECRET, "status": "enabled"}).encode(),
        ),
    )
    expect_ok(
        "policy attached",
        signed_request(
            "PUT",
            "/rustfs/admin/v3/set-user-or-group-policy",
            {"policyName": POLICY_NAME, "userOrGroup": SERVICE_USER, "isGroup": "false"},
        ),
    )
    print("storage initialised")


if __name__ == "__main__":
    sys.exit(main())
