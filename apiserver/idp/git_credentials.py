"""Git credentials for hosts other than GitHub, stored encrypted in the backend database.

The encryption key is the secret git_credentials_key in the secrets directory: never the database, never the
repository. Credentials are write-only: the API can set, replace, delete and list them, but no call returns a secret.
A secret is decrypted only to answer devices.git_token for the one repository of a task, and is never logged.
"""

import base64
import hashlib
import re
import uuid
from datetime import datetime
from typing import Iterable, Optional, Tuple
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken

from apiserver.apierrors import errors
from apiserver.database.model.git_credential import GitCredential
from apiserver.idp.audit import record as audit
from apiserver.idp.secrets import SecretMissing, read_secret

KEY_SECRET = "git_credentials_key"
_HOST = re.compile(r"^[a-z0-9.-]+(:[0-9]+)?$")
_BAD_PATH_CHARS = re.compile(r"[\s?#@\\]")


def key_material() -> str:
    """The secret the encryption key is derived from. Refused when it is missing or empty."""
    try:
        value = read_secret(KEY_SECRET)
    except SecretMissing:
        value = ""
    if not value:
        raise errors.server_error.ConfigError(
            reason=f"the secret {KEY_SECRET} is missing from the secrets directory: Git credentials cannot be "
                   "stored or used without it")
    return value


def fernet_for(material: str) -> Fernet:
    """The cipher for a key secret. The secret is random (openssl rand), so one SHA-256 turns it into the 32-byte key
    Fernet needs. Fernet encrypts (AES-128-CBC) and authenticates (HMAC-SHA256) every value."""
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(material.encode("utf-8")).digest()))


def encrypt(value: str, material: str) -> str:
    return fernet_for(material).encrypt(value.encode("utf-8")).decode("ascii")


class CredentialUnreadable(Exception):
    """A stored secret does not decrypt with the key on this server. Never carries the value."""


def decrypt(token: str, material: str) -> str:
    try:
        return fernet_for(material).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError):
        raise CredentialUnreadable() from None


def normalise_host(host: str) -> str:
    value = (host or "").strip().lower()
    if not _HOST.match(value):
        raise errors.bad_request.ValidationError(
            "host must be a host name, optionally with a port, such as gitlab.example.com")
    return value


def normalise_path(path: str) -> str:
    value = (path or "").strip().strip("/").lower()
    if value:
        segments = value.split("/")
        if any(segment in ("", ".", "..") or _BAD_PATH_CHARS.search(segment) for segment in segments):
            raise errors.bad_request.ValidationError("path must be a plain path prefix, such as team/ or team/project")
    return value


def host_and_path(repository: str) -> Tuple[str, str]:
    """The host (with its port) and the path of a repository URL, in lower case, without user information,
    '.git' or slashes at either end. A URL without a scheme is taken as HTTPS."""
    parts = urlsplit(repository if "://" in repository else f"https://{repository}")
    host = parts.netloc.rsplit("@", 1)[-1].lower()
    path = parts.path.strip("/").lower()
    if path.endswith(".git"):
        path = path[:-4]
    return host, path.strip("/")


def covers(prefix: str, path: str) -> bool:
    """Whether a stored path prefix covers a repository path. Matching is by whole path segments."""
    return prefix == "" or path == prefix or path.startswith(prefix + "/")


def most_specific(entries: Iterable, path: str):
    """The entry with the longest path prefix covering the path, or None."""
    matches = [entry for entry in entries if covers(entry.path, path)]
    return max(matches, key=lambda entry: len(entry.path), default=None)


def _view(entry: GitCredential) -> dict:
    """What the API may say about a stored credential: never the secret."""
    return {
        "host": entry.host,
        "path": entry.path,
        "username": entry.username,
        "updated": entry.updated.isoformat() if entry.updated else None,
    }


def set_credential(company: str, host: str, path: str, username: str, secret: str, actor: str) -> dict:
    host, path = normalise_host(host), normalise_path(path)
    if not username or not secret:
        raise errors.bad_request.ValidationError("username and secret are required")
    encrypted = encrypt(secret, key_material())
    now = datetime.utcnow()
    entry = GitCredential.objects(company=company, host=host, path=path).first()
    if entry is None:
        entry = GitCredential(id=uuid.uuid4().hex, company=company, host=host, path=path, created=now)
    entry.username = username
    entry.secret = encrypted
    entry.updated = now
    entry.save()
    audit(actor, "git_credential.set", host=host, path=path)
    return _view(entry)


def delete_credential(company: str, host: str, path: str, actor: str) -> int:
    host, path = normalise_host(host), normalise_path(path)
    deleted = GitCredential.objects(company=company, host=host, path=path).delete()
    if deleted:
        audit(actor, "git_credential.deleted", host=host, path=path)
    return deleted


def list_credentials(company: str) -> list:
    return [_view(entry) for entry in GitCredential.objects(company=company).order_by("host", "path")]


def find_credential(company: str, repository: str) -> Optional[GitCredential]:
    """The stored credential that applies to a repository URL, or None."""
    host, path = host_and_path(repository)
    return most_specific(GitCredential.objects(company=company, host=host), path)


def secret_of(entry: GitCredential) -> str:
    """The decrypted secret, for an answer to devices.git_token only."""
    try:
        return decrypt(entry.secret, key_material())
    except CredentialUnreadable:
        raise errors.server_error.ConfigError(
            reason=f"the stored Git credential for {entry.host} cannot be read with the current {KEY_SECRET}") from None
