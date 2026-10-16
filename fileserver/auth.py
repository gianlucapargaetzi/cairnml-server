import json
from hashlib import sha256
from typing import Optional

import attr
import requests
from flask import abort, Response, Request
from werkzeug.exceptions import HTTPException

from config import config
from redis_manager import redman

log = config.logger(__file__)


@attr.s(auto_attribs=True)
class TokenInfo:
    company: str
    user: str


def fetch_token_info(api_server: str, token: str) -> TokenInfo:
    """Ask the API server whom the caller's own token belongs to.

    The file server holds no credential of its own: the token is the caller's, and the API server
    applies its checks to it (a revoked device or a disabled user is refused there). Raises an
    HTTPException for a refused token (401) or a failed check (500).
    """
    try:
        res = requests.post(
            f"{api_server}/v2.8/users.get_current_user",
            headers={"Authorization": f"Bearer {token}"},
            json={},
            timeout=10,
        )
    except requests.RequestException:
        log.exception("Could not reach the API server to check a token")
        abort(500)

    if res.status_code in (401, 403):
        abort(401)
    if res.status_code != 200:
        log.error(f"Token check failed (status={res.status_code})")
        abort(500)

    user = res.json()["data"]["user"]
    return TokenInfo(company=user["company"]["id"], user=user["id"])


class AuthHandler:
    enabled = config.get("fileserver.auth.enabled", False)
    _instance = None

    @classmethod
    def instance(cls):
        if not cls.enabled:
            return None
        if not cls._instance:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self.api_server = config.get("hosts.api_server")
        self.redis = redman.connection("fileserver")

    def _validate_and_get_token_info(self, token: str) -> TokenInfo:
        token_hash = sha256(token.encode()).hexdigest() if len(token) > 256 else token
        key = f"token_{token_hash}"
        token_data = self.redis.get(key)
        if token_data:
            return TokenInfo(**json.loads(token_data))

        try:
            info = fetch_token_info(self.api_server, token)
        except HTTPException:
            raise
        except Exception:
            log.exception("Failed checking a token")
            abort(500)

        # Cached only briefly (fileserver.auth.tokens_cache_threshold_sec): a revoked device must lose
        # file access within about a minute, not after the cache has run out.
        timeout_sec = config.get("fileserver.auth.tokens_cache_threshold_sec", 60)
        self.redis.setex(key, time=timeout_sec, value=json.dumps(attr.asdict(info)))
        return info

    def validate(self, request: Request):
        token = self.get_token(request)
        if not token:
            log.error("Error getting token")
            abort(401)

        self._validate_and_get_token_info(token)

    @staticmethod
    def get_token(request: Request) -> Optional[str]:
        auth_header = request.headers.get("Authorization")
        if auth_header:
            if not auth_header.startswith("Bearer "):
                log.error("Only bearer token authorization is supported")
                abort(
                    Response("Only bearer token authorization is supported", status=401)
                )
            token = auth_header.partition(" ")[2]
            return token

        last_ex = None
        for cookie_name in config.get("fileserver.auth.cookie_names", []):
            cookie = request.cookies.get(cookie_name)
            if not cookie:
                continue
            try:
                return cookie
            except HTTPException as ex:
                last_ex = ex

        if last_ex:
            raise last_ex
