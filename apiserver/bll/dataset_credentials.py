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

    A kept dataset is checked as its visibility and owner allow (R13): ``read`` needs it visible to the caller,
    ``read_write`` needs the caller to own it or to be an admin. A caller who sees a dataset and one who does not get
    the same answer for a missing one, so a hidden dataset does not reveal itself.

    A repository with no kept record (R14) is new: ``read_write`` gets keys under the caller's own company prefix,
    so that a dataset can be created through the CLI before it is registered. ``read`` is refused. A name that some
    other record in the company already has is never opened this way.
    """
    if level not in LEVELS:
        raise errors.bad_request.ValidationError("the level is read or read_write")
    if not _valid_repository(repository):
        raise errors.bad_request.ValidationError("the repository is owner/name on GitHub, with letters, digits, dots, dashes and underscores")
    if not SEGMENT.fullmatch(company or ""):
        raise errors.bad_request.ValidationError("the company id is not a valid key segment")
    dataset = Dataset.objects(company=company, kind="repo", ref__iexact=repository).first()
    if dataset is None:
        taken = Dataset.objects(company=company, ref__iexact=repository).first() is not None
        if level == "read" or taken:
            raise errors.bad_request.ValidationError("no such dataset")
        return prefix_of(company, repository)
    as_dict = {"owner": dataset.owner, "visibility": dataset.visibility, "projects": dataset.projects}
    if level == "read":
        user_projects = {project["id"] for project in user_access.my_projects(company, user)}
        if not datasets_bll.visible_to(as_dict, user, user_projects, is_admin):
            raise errors.bad_request.ValidationError("no such dataset")
    elif not datasets_bll.can_manage(as_dict, user, is_admin):
        raise errors.forbidden.NoWritePermission("only the owner or an admin may write to a dataset")
    return prefix_of(company, dataset.ref)


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
