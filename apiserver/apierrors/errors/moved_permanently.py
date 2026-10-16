from apiserver.apierrors.base import BaseError


class NotSupported(BaseError):
    _default_code = 301
    _default_subcode = 1
    _default_msg = "This endpoint is no longer supported for the requested api version"
