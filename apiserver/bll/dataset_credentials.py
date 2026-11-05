"""Short-lived keys for one dataset's bucket prefix, at one level.

Dataset repositories keep their files in this bucket through DVC, which speaks S3 itself and so needs a key, not a
presigned URL. The server assumes a session policy that covers only the prefix of the dataset asked for, and only the
actions of the level the caller may use: ``read`` lists and reads the dataset's objects, ``read_write`` also writes them.
A key that leaks reaches nothing else, and stops working when it expires.

The check on the caller is the one that goes with the dataset's visibility until the share levels exist: ``read`` needs
the dataset to be visible to the caller, and ``read_write`` needs to be the dataset's owner or an admin.
"""

import json
import re
from datetime import datetime, timezone
from functools import lru_cache

import boto3
from botocore.config import Config

from apiserver.apierrors import errors
from apiserver.bll import datasets as datasets_bll
from apiserver.bll import user_access
from apiserver.config_repo import config
from apiserver.database.model.dataset import Dataset
from apiserver.idp import public_origin
from apiserver.idp.secrets import read_secret

BUCKET = "dataset"
LEVELS = ("read", "read_write")
REPOSITORY = re.compile(r"^[^/\s]+/[^/\s]+$")

READ_ACTIONS = ["s3:GetObject"]
WRITE_ACTIONS = ["s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]


def prefix_of(company: str, repository: str) -> str:
    """The bucket path of a dataset, bucket name first: dataset/<company>/<owner>/<name>."""
    return f"{BUCKET}/{company}/{repository}"


def session_policy(prefix: str, level: str) -> dict:
    """The session policy for one dataset prefix (``bucket/key/path``) at one level."""
    if level not in LEVELS:
        raise errors.bad_request.ValidationError("the level is read or read_write")
    bucket, _, key = prefix.partition("/")
    actions = READ_ACTIONS + (WRITE_ACTIONS if level == "read_write" else [])
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": actions,
                "Resource": [f"arn:aws:s3:::{bucket}/{key}/*"],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": [f"arn:aws:s3:::{bucket}"],
                "Condition": {"StringLike": {"s3:prefix": [key, f"{key}/*"]}},
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetBucketLocation"],
                "Resource": [f"arn:aws:s3:::{bucket}"],
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


def _authorize(company: str, user: str, repository: str, level: str, is_admin: bool) -> Dataset:
    """The kept dataset for the repository, when the caller may use the level. A dataset the caller cannot see gets
    the same answer as a missing one, so a hidden dataset does not reveal itself."""
    if level not in LEVELS:
        raise errors.bad_request.ValidationError("the level is read or read_write")
    if not REPOSITORY.match(repository or ""):
        raise errors.bad_request.ValidationError("the repository is owner/name on GitHub")
    dataset = Dataset.objects(company=company, kind="repo", ref__iexact=repository).first()
    if dataset is None:
        raise errors.bad_request.ValidationError("no such dataset")
    as_dict = {"owner": dataset.owner, "visibility": dataset.visibility, "projects": dataset.projects}
    if level == "read":
        user_projects = {project["id"] for project in user_access.my_projects(company, user)}
        if not datasets_bll.visible_to(as_dict, user, user_projects, is_admin):
            raise errors.bad_request.ValidationError("no such dataset")
    elif not datasets_bll.can_manage(as_dict, user, is_admin):
        raise errors.forbidden.NoWritePermission("only the owner or an admin may write to a dataset")
    return dataset


def dataset_credentials(company: str, user: str, repository: str, level: str, is_admin: bool = False) -> dict:
    """Temporary keys for one dataset's prefix at one level, with the public address of the store and the prefix."""
    dataset = _authorize(company, user, repository, level, is_admin)
    prefix = prefix_of(company, dataset.ref)
    ttl = _ttl()
    issued = _sts().assume_role(
        RoleArn="arn:aws:iam::rustfs:role/dataset",
        RoleSessionName="dataset",
        DurationSeconds=ttl,
        Policy=json.dumps(session_policy(prefix, level)),
    )["Credentials"]
    expires = issued["Expiration"]
    if isinstance(expires, datetime):
        expires = expires.astimezone(timezone.utc).isoformat()
    return {
        "endpoint": public_origin.storage_endpoint(),
        "region": config.get("apiserver.storage.region"),
        "bucket": BUCKET,
        "prefix": prefix,
        "access_key": issued["AccessKeyId"],
        "secret_key": issued["SecretAccessKey"],
        "session_token": issued["SessionToken"],
        "expires_at": expires,
    }
