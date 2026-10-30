"""Plain REST calls to the CairnML API, as an enrolled device.

The device signs a short assertion with its key; the server exchanges it for a token (devices.exchange).
This is what the SDK does, written out here so the tests can also send requests the SDK would never send
(a revoked key, a forged assertion, another account's objects).
"""

import time
import uuid
from pathlib import Path

import jwt
import requests
from cryptography.hazmat.primitives import serialization

from . import settings

API_VERSION = "2.8"


class ApiError(Exception):
    def __init__(self, status: int, message: str, endpoint: str):
        super().__init__(f"{endpoint}: {status} {message}")
        self.status = status
        self.message = message
        self.endpoint = endpoint


def load_device_key(key_path: Path):
    return serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)


def make_assertion(key, device_id: str, audience: str, *, ttl: int = 60) -> str:
    now = int(time.time())
    claims = {
        "iss": device_id,
        "sub": device_id,
        "aud": audience.rstrip("/"),
        "iat": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, key, algorithm="EdDSA")


class Api:
    """Calls the API as one device. Tokens are fetched per call, as the device would."""

    def __init__(self, device_id: str, key_path: Path, http=None):
        self.device_id = device_id
        self.key = load_device_key(key_path)
        self.http = http or requests.Session()

    def base(self, version: str = API_VERSION) -> str:
        return f"{settings.API_URL}/v{version}/"

    def call(self, endpoint: str, body=None, *, token=None, raw_token=None, version: str = API_VERSION) -> dict:
        base = self.base(version)
        headers = {}
        if raw_token is not None:
            headers["Authorization"] = raw_token
        elif token is not None:
            headers["Authorization"] = f"Bearer {token}"
        reply = self.http.post(base + endpoint, json=body or {}, headers=headers, timeout=60)
        try:
            payload = reply.json()
        except ValueError:
            payload = {}
        if reply.status_code != 200:
            message = payload.get("meta", {}).get("result_msg") or reply.text[:200]
            raise ApiError(reply.status_code, message, endpoint)
        return payload.get("data", {})

    def token(self) -> str:
        assertion = make_assertion(self.key, self.device_id, settings.API_URL)
        return self.call("devices.exchange", {"assertion": assertion})["access_token"]

    def authed(self, endpoint: str, body=None, *, version: str = API_VERSION) -> dict:
        return self.call(endpoint, body, token=self.token(), version=version)
