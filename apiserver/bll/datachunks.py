"""Object storage for artifacts, datasets, models and debug data.

The server signs one upload or download for one object and the client moves the bytes itself. Every
upload is recorded first as a Datachunk, and a commit checks the stored object against that record.
"""

import base64
import re
import uuid
from datetime import datetime, timedelta
from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from apiserver.apierrors import errors
from apiserver.config_repo import config
from apiserver.database.model.datachunk import Datachunk, DatachunkKind, DatachunkState
from apiserver.idp.secrets import read_secret

# One presigned PUT carries at most this many bytes. Larger objects need multipart upload, which is later work.
MAX_SINGLE_PUT_BYTES = 5 * 1024**3

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
DATASET_TAG = "dataset"


def sanitise_name(name: str) -> str:
    """A relative object name: any printable characters, no empty, dot or dot-dot parts, no leading slash."""
    if not name or len(name) > 1024 or _CONTROL_RE.search(name):
        raise errors.bad_request.ValidationError("invalid name", name=name)
    if name.startswith("/") or any(part in ("", ".", "..") for part in name.split("/")):
        raise errors.bad_request.ValidationError("invalid name", name=name)
    return name


def validate_digest(sha256: str) -> str:
    if not sha256 or not _SHA256_RE.fullmatch(sha256):
        raise errors.bad_request.ValidationError("sha256 must be 64 lowercase hex characters")
    return sha256


def build_key(project: str, task_id: str, name: str) -> str:
    """The key the server gives the object. The client never chooses a prefix."""
    return f"{project or '_no_project'}/{task_id}/{name}"


def _b64_digest(sha256_hex: str) -> str:
    return base64.b64encode(bytes.fromhex(sha256_hex)).decode()


@lru_cache(maxsize=2)
def _s3(endpoint: str):
    """An S3 client for one endpoint. The secret is read from the secrets folder when first needed."""
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=config.get("apiserver.storage.access_key"),
        aws_secret_access_key=read_secret("s3_secret_key"),
        region_name=config.get("apiserver.storage.region"),
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _internal():
    """Used by the server itself: head, delete."""
    return _s3(config.get("apiserver.storage.endpoint"))


def _signer():
    """Used only to sign URLs. Signing needs no network, so this client may use the public address."""
    return _s3(config.get("apiserver.storage.public_endpoint"))


def _ttl() -> int:
    return int(config.get("apiserver.storage.presign_ttl_sec"))


def check_kind_allowed(kind: str, task_tags) -> DatachunkKind:
    try:
        parsed = DatachunkKind(kind)
    except ValueError:
        raise errors.bad_request.ValidationError("unknown kind", kind=kind) from None
    if parsed == DatachunkKind.dataset and DATASET_TAG not in (task_tags or []):
        raise errors.forbidden.NoWritePermission("dataset data needs a dataset task")
    return parsed


def prepare_upload(
    *,
    company: str,
    task_id: str,
    project: str,
    task_tags,
    kind: str,
    name: str,
    size: int,
    sha256: str,
    content_type: str = "",
) -> dict:
    """Records a pending Datachunk and returns a presigned PUT for its bytes."""
    kind_enum = check_kind_allowed(kind, task_tags)
    name = sanitise_name(name)
    sha256 = validate_digest(sha256)
    if not isinstance(size, int) or size < 0:
        raise errors.bad_request.ValidationError("size must be a non-negative integer")
    if size > MAX_SINGLE_PUT_BYTES:
        raise errors.bad_request.ValidationError(
            "object is larger than one upload; multipart upload is not available yet", size=size
        )

    key = build_key(project, task_id, name)
    bucket = kind_enum.value
    chunk = Datachunk(
        id=uuid.uuid4().hex,
        company=company,
        task=task_id,
        project=project or "",
        kind=kind_enum,
        bucket=bucket,
        key=key,
        name=name,
        size=size,
        sha256=sha256,
        content_type=content_type or "",
        state=DatachunkState.pending,
        created=datetime.utcnow(),
    )
    chunk.save()

    checksum = _b64_digest(sha256)
    params = {"Bucket": bucket, "Key": key, "ContentLength": size, "ChecksumSHA256": checksum}
    headers = {"x-amz-checksum-sha256": checksum}
    if content_type:
        params["ContentType"] = content_type
        headers["Content-Type"] = content_type
    url = _signer().generate_presigned_url("put_object", Params=params, ExpiresIn=_ttl())
    return {
        "id": chunk.id,
        "method": "PUT",
        "url": url,
        "headers": headers,
        "expires_at": (datetime.utcnow() + timedelta(seconds=_ttl())).isoformat(),
    }


def commit(*, company: str, chunk_id: str) -> Datachunk:
    """Checks the stored object against its record. A mismatch deletes the object and fails the commit."""
    chunk = Datachunk.objects(id=chunk_id, company=company).first()
    if not chunk:
        raise errors.bad_request.InvalidId("datachunk not found", id=chunk_id)
    if chunk.state == DatachunkState.stored:
        return chunk

    client = _internal()
    try:
        head = client.head_object(Bucket=chunk.bucket, Key=chunk.key, ChecksumMode="ENABLED")
    except ClientError:
        raise errors.bad_request.ValidationError("object is not uploaded yet", id=chunk_id) from None

    problems = []
    if head.get("ContentLength") != chunk.size:
        problems.append("size")
    if head.get("ChecksumSHA256") != _b64_digest(chunk.sha256):
        problems.append("sha256")
    if problems:
        client.delete_object(Bucket=chunk.bucket, Key=chunk.key)
        chunk.delete()
        raise errors.bad_request.ValidationError("stored object does not match the record", mismatch=problems)

    chunk.state = DatachunkState.stored
    chunk.stored = datetime.utcnow()
    chunk.save()
    return chunk


def find_stored(*, company: str, task_id: str, kind: str, name: str) -> Datachunk:
    """The newest stored object for a task, kind and name. Re-uploads replace it once committed."""
    chunk = (
        Datachunk.objects(company=company, task=task_id, kind=kind, name=name, state=DatachunkState.stored)
        .order_by("-stored")
        .first()
    )
    if not chunk:
        raise errors.bad_request.InvalidId("no stored object with this name", task=task_id, kind=kind, name=name)
    return chunk


def prepare_download(*, company: str, chunk: Datachunk) -> dict:
    if chunk.state != DatachunkState.stored:
        raise errors.bad_request.ValidationError("datachunk is not stored", id=chunk.id)
    url = _signer().generate_presigned_url(
        "get_object", Params={"Bucket": chunk.bucket, "Key": chunk.key}, ExpiresIn=_ttl()
    )
    return {
        "id": chunk.id,
        "url": url,
        "size": chunk.size,
        "sha256": chunk.sha256,
        "expires_at": (datetime.utcnow() + timedelta(seconds=_ttl())).isoformat(),
    }


def delete(*, company: str, chunks) -> int:
    """Removes the objects and their records. A missing object is not an error: the record goes anyway."""
    client = _internal()
    removed = 0
    for chunk in chunks:
        if chunk.company != company:
            continue
        try:
            client.delete_object(Bucket=chunk.bucket, Key=chunk.key)
        except ClientError:
            pass
        chunk.delete()
        removed += 1
    return removed


def delete_objects(bucket_key_pairs) -> None:
    """Removes objects from the store. Used by the async delete job."""
    client = _internal()
    for bucket, key in bucket_key_pairs:
        client.delete_object(Bucket=bucket, Key=key)
