from apiserver.apierrors.base import BaseError


class NotAuthorized(BaseError):
    _default_code = 401
    _default_subcode = 1
    _default_msg = "Unauthorized (not authorized for endpoint)"


class EntityNotAllowed(BaseError):
    _default_code = 401
    _default_subcode = 2
    _default_msg = "Unauthorized (entity not allowed)"


class BadAuthType(BaseError):
    _default_code = 401
    _default_subcode = 10
    _default_msg = "Unauthorized (bad authentication header type)"


class NoCredentials(BaseError):
    _default_code = 401
    _default_subcode = 20
    _default_msg = "Unauthorized (missing credentials)"


class BadCredentials(BaseError):
    _default_code = 401
    _default_subcode = 21
    _default_msg = "Unauthorized (malformed credentials)"


class InvalidCredentials(BaseError):
    _default_code = 401
    _default_subcode = 22
    _default_msg = "Unauthorized (invalid credentials)"


class InvalidToken(BaseError):
    _default_code = 401
    _default_subcode = 30
    _default_msg = "Invalid token"


class BlockedToken(BaseError):
    _default_code = 401
    _default_subcode = 31
    _default_msg = "Token is blocked"


class InvalidFixedUser(BaseError):
    _default_code = 401
    _default_subcode = 40
    _default_msg = "Fixed user id was not found"
