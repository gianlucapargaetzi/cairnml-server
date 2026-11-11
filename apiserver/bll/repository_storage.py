"""Short-lived keys for one dataset's bucket prefix, at one level.

Dataset repositories keep their files in this bucket through DVC, which speaks S3 itself and so needs a key, not a
presigned URL. The server assumes a session policy that covers only the prefix of the dataset asked for, and only the
actions of the level the caller may use: ``read`` lists and reads the dataset's objects, ``read_write`` also writes them.
A key that leaks reaches nothing else, and stops working when it expires.

The caller's level on the dataset decides: ``read`` needs the use level (read), and ``read_write`` the change_content
level (read/write), as bll/access.ACTION_NEEDS sets it.
"""

import json
import re
from datetime import datetime, timezone
from functools import lru_cache

import boto3
from botocore.config import Config

from apiserver.apierrors import errors
from apiserver.bll import repositories as repos
from apiserver.config_repo import config
from apiserver.idp import public_origin
from apiserver.idp.secrets import read_secret

BUCKET = "dataset"
LEVELS = ("read", "read_write")
SEGMENT = re.compile(r"[A-Za-z0-9._-]+")
""" one part of owner/name: letters, digits, dot, dash, underscore; never a wildcard or a path step """

READ_ACTIONS = ["s3:GetObject"]
WRITE_ACTIONS = ["s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]


def _valid_repository(repository) -> bool:
    """True for owner/name with safe characters only. fullmatch refuses a trailing newline; '.' and '..' are refused
    as segments so that no key can step out of the company prefix."""
    if not isinstance(repository, str):
        return False
    match = re.fullmatch(r"([^/]+)/([^/]+)", repository)
    if match is None:
        return False
    return all(SEGMENT.fullmatch(part) and part not in (".", "..") for part in match.groups())


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


def _authorize(company: str, user: str, repository: str, level: str, is_admin: bool) -> str:
    """The bucket prefix the keys reach, when the caller may use the level.

    A kept repository is checked by what the caller holds on it (bll/repositories.level_of): ``read`` needs to see it,
    ``read_write`` to own it (or be the admin). A caller who sees a repository and one who does not get the same answer for a
    missing one, so a hidden dataset does not reveal itself.

    A repository with no record is new: ``read_write`` gets keys under the caller's own company prefix, so that a dataset can be
    created through the CLI before it is listed. ``read`` is refused.
    """
    if level not in LEVELS:
        raise errors.bad_request.ValidationError("the level is read or read_write")
    if not _valid_repository(repository):
        raise errors.bad_request.ValidationError("the repository is owner/name on GitHub, with letters, digits, dots, dashes and underscores")
    if not SEGMENT.fullmatch(company or ""):
        raise errors.bad_request.ValidationError("the company id is not a valid key segment")
    repo = repos.find(company, f"https://github.com/{repository}")
    if repo is None:
        if level == "read":
            raise errors.bad_request.ValidationError("no such dataset")
        return prefix_of(company, repository)
    held = repos.level_of(company, user, is_admin, repo)
    if held is None:
        raise errors.bad_request.ValidationError("no such dataset")
    if level == "read_write" and held != "collaborator":
        raise errors.forbidden.NoWritePermission("only the owner of a dataset or the admin may write to it")
    return prefix_of(company, repo.path)


def dataset_credentials(company: str, user: str, repository: str, level: str, is_admin: bool = False) -> dict:
    """Temporary keys for one dataset's prefix at one level, with the public address of the store and the prefix."""
    prefix = _authorize(company, user, repository, level, is_admin)
    if any(ch in prefix for ch in "*?[]"):  # the prefix is a key path: a wildcard in it would widen the policy
        raise errors.bad_request.ValidationError("the dataset prefix is not a plain path")
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
