from time import time

import jwt

from apiserver.apierrors import errors
from apiserver.config_repo import config
from apiserver.database.model.auth import Entities
from apiserver.database.model.company import Company
from apiserver.database.utils import get_options
from apiserver.redis_manager import redman
from .payload import Payload, Token, AuthType

log = config.logger(__file__)
entity_keys = set(get_options(Entities))
_revoked_tokens_key = "revoked_tokens"
redis = redman.connection("apiserver")


def get_auth_func(auth_type):
    # Only identity-provider tokens are accepted; access keys and passwords are gone.
    if auth_type == AuthType.bearer_token:
        return authorize_token
    raise errors.unauthorized.BadAuthType()


def authorize_token(jwt_token, service, action, call):
    """Validate token against service/endpoint and requests data (dicts).
    Returns a parsed token object (auth payload)
    """
    call_info = {"ip": call.real_ip}

    def log_error(msg):
        info = ", ".join(f"{k}={v}" for k, v in call_info.items())
        log.error(f"{msg} Call info: {info}")

    try:
        token = Token.from_encoded_token(jwt_token)
        if is_token_revoked(token):
            raise errors.unauthorized.InvalidToken("revoked token")
        check_idp_token(jwt_token)
        # a job token names its task, and validate_job_scope keeps the job to that task
        claims = Token.decode(jwt_token)
        token.job_task = claims.get("task") if claims.get("kind") == "job" else None
        return token
    except jwt.exceptions.InvalidKeyError as ex:
        log_error("Failed parsing token.")
        raise errors.unauthorized.InvalidToken(
            "jwt invalid key error", reason=ex.args[0]
        )
    except jwt.InvalidTokenError as ex:
        log_error("Failed parsing token.")
        raise errors.unauthorized.InvalidToken("invalid jwt token", reason=ex.args[0])
    except ValueError as ex:
        log_error(f"Failed while processing token: {str(ex.args[0])}.")
        raise errors.unauthorized.InvalidToken(
            "failed processing token", reason=ex.args[0]
        )
    except Exception:
        log_error("Failed processing token.")
        raise


def authorize_impersonation(user, identity, service, action, call):
    """ Returns a new basic object (auth payload)"""
    if not user:
        raise ValueError("missing user")

    company = Company.objects(id=user.company).only("id", "name").first()
    if not company:
        raise errors.unauthorized.InvalidCredentials("invalid user company")

    return Payload(auth_type=None, identity=identity)


def check_idp_token(jwt_token: str) -> None:
    """Device and job tokens are refused when their device, owner or task no longer allows them.

    Tokens without a kind are refused: every token is now issued by the identity-provider flow.
    """
    from apiserver.database.model.auth import User
    from apiserver.database.model.device import Device
    from apiserver.database.model.task.task import Task
    from apiserver.idp.checks import DEVICE_KINDS, AccessDenied, check_device, check_job, check_user

    claims = Token.decode(jwt_token)
    kind = claims.get("kind")
    if kind is None:
        raise errors.unauthorized.InvalidToken("token is not an identity-provider token")
    try:
        if kind in DEVICE_KINDS:
            device = Device.objects(id=claims.get("device")).first()
            owner = User.objects(id=device.user).first() if device else None
            check_device(device, owner)
        elif kind == "job":
            task = Task.objects(id=claims.get("task")).only("status", "user").first()
            owner = User.objects(id=task.user).first() if task else None
            check_job(task.status if task else None, owner)
        elif kind == "web":
            owner = User.objects(id=claims["identity"]["user"]).first()
            check_user(owner)
    except AccessDenied as exc:
        raise errors.unauthorized.InvalidToken(str(exc)) from None


def is_token_revoked(token: Token) -> bool:
    if not isinstance(token, Token) or not token.session_id:
        return False

    return redis.zscore(_revoked_tokens_key, token.session_id) is not None


def revoke_auth_token(token: Token):
    if not isinstance(token, Token) or not token.session_id:
        return

    timestamp_now = int(time())
    expiration_timestamp = token.exp
    if not expiration_timestamp:
        expiration_timestamp = timestamp_now + Token.default_expiration_sec

    redis.zadd(_revoked_tokens_key, {token.session_id: expiration_timestamp})
    redis.zremrangebyscore(_revoked_tokens_key, min=0, max=timestamp_now)
