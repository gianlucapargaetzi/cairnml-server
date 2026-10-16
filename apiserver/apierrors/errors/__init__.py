from apiserver.apierrors.base import BaseError

from . import moved_permanently
from . import bad_request
from . import unauthorized
from . import forbidden
from . import gone
from . import server_error


class MovedPermanently(BaseError):
    _default_code = 301
    _default_subcode = 0
    _default_msg = "Moved permanently"


class BadRequest(BaseError):
    _default_code = 400
    _default_subcode = 0
    _default_msg = "Bad request"


class Unauthorized(BaseError):
    _default_code = 401
    _default_subcode = 0
    _default_msg = "Unauthorized"


class Forbidden(BaseError):
    _default_code = 403
    _default_subcode = 0
    _default_msg = "Forbidden"


class Gone(BaseError):
    _default_code = 410
    _default_subcode = 0
    _default_msg = "Gone"


class ServerError(BaseError):
    _default_code = 500
    _default_subcode = 0
    _default_msg = "Server error"


