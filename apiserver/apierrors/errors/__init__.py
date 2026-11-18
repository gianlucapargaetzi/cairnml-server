from apiserver.apierrors.base import BaseError

from . import moved_permanently  # noqa: F401 (registers the error classes of the section)
from . import bad_request  # noqa: F401 (registers the error classes of the section)
from . import unauthorized  # noqa: F401 (registers the error classes of the section)
from . import forbidden  # noqa: F401 (registers the error classes of the section)
from . import not_found  # noqa: F401 (registers the error classes of the section)
from . import gone  # noqa: F401 (registers the error classes of the section)
from . import server_error  # noqa: F401 (registers the error classes of the section)


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


class NotFound(BaseError):
    _default_code = 404
    _default_subcode = 0
    _default_msg = "Not found"


class Gone(BaseError):
    _default_code = 410
    _default_subcode = 0
    _default_msg = "Gone"


class ServerError(BaseError):
    _default_code = 500
    _default_subcode = 0
    _default_msg = "Server error"


