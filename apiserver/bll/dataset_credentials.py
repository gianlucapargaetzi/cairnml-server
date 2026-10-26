"""Short-lived keys for the dataset bucket.

Dataset repositories keep their files in this bucket through DVC, which speaks S3 itself and so needs a
key, not a presigned URL. The server assumes a session policy that allows only reading and writing objects
in the dataset bucket (and listing it), so a key that leaks can reach nothing else and stops working when
it expires.
"""

import json
from datetime import datetime, timezone
from functools import lru_cache

import boto3
from botocore.config import Config

from apiserver.config_repo import config
from apiserver.idp import public_origin
from apiserver.idp.secrets import read_secret

BUCKET = "dataset"

SESSION_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject",
                "s3:PutObject",
                "s3:AbortMultipartUpload",
                "s3:ListMultipartUploadParts",
            ],
            "Resource": [f"arn:aws:s3:::{BUCKET}/*"],
        },
        {
            "Effect": "Allow",
            "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
            "Resource": [f"arn:aws:s3:::{BUCKET}"],
        },
    ],
}


@lru_cache(maxsize=1)
def _sts():
    """An STS client for the store's internal address. The secret is read when first needed."""
    return boto3.client(
        "sts",
        endpoint_url=config.get("apiserver.storage.endpoint"),
        aws_access_key_id=config.get("apiserver.storage.access_key"),
        aws_secret_access_key=read_secret("s3_secret_key"),
        region_name=config.get("apiserver.storage.region"),
        config=Config(signature_version="v4"),
    )


def _ttl() -> int:
    return int(config.get("apiserver.storage.dataset_credentials_ttl_sec"))


def dataset_credentials() -> dict:
    """Temporary keys for the dataset bucket, with the public address of the store."""
    ttl = _ttl()
    issued = _sts().assume_role(
        RoleArn="arn:aws:iam::rustfs:role/dataset",
        RoleSessionName="dataset",
        DurationSeconds=ttl,
        Policy=json.dumps(SESSION_POLICY),
    )["Credentials"]
    expires = issued["Expiration"]
    if isinstance(expires, datetime):
        expires = expires.astimezone(timezone.utc).isoformat()
    return {
        "endpoint": public_origin.storage_endpoint(),
        "region": config.get("apiserver.storage.region"),
        "bucket": BUCKET,
        "access_key": issued["AccessKeyId"],
        "secret_key": issued["SecretAccessKey"],
        "session_token": issued["SessionToken"],
        "expires_at": expires,
    }
