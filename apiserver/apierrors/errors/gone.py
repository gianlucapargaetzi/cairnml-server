from apiserver.apierrors.base import BaseError


class NotSupported(BaseError):
    _default_code = 410
    _default_subcode = 1
    _default_msg = "Thus endpoint is not supported any more"
