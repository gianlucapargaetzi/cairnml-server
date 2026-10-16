from apiserver.apierrors.base import BaseError


class RoutingError(BaseError):
    _default_code = 403
    _default_subcode = 10
    _default_msg = "Forbidden (routing error)"


class BlockedInternalEndpoint(BaseError):
    _default_code = 403
    _default_subcode = 12
    _default_msg = "Forbidden (blocked internal endpoint)"


class RoleNotAllowed(BaseError):
    _default_code = 403
    _default_subcode = 20
    _default_msg = "Forbidden (not allowed for role)"


class NoWritePermission(BaseError):
    _default_code = 403
    _default_subcode = 21
    _default_msg = "Forbidden (modification not allowed)"
