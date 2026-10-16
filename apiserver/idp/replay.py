"""Each assertion carries a unique id (jti). It may be used once, which stops replayed assertions."""

from apiserver.redis_manager import redman

_redis = redman.connection("apiserver")
KEY_PREFIX = "idp:jti:"


def claim_jti(jti: str, ttl: int, client=None) -> bool:
    """True the first time a jti is seen within ttl seconds; False on any reuse."""
    client = client or _redis
    return bool(client.set(f"{KEY_PREFIX}{jti}", "1", nx=True, ex=ttl))
