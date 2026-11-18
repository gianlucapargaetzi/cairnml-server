from apiserver.apierrors.base import BaseError


class UnsupportedEndpointVersion(BaseError):
    _default_code = 404
    _default_subcode = 1
    _default_msg = "Endpoint is not available at the requested api version"
