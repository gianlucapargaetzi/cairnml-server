from apiserver.apierrors.base import BaseError


class GeneralError(BaseError):
    _default_code = 500
    _default_subcode = 0
    _default_msg = "General server error"


class InternalError(BaseError):
    _default_code = 500
    _default_subcode = 1
    _default_msg = "Internal server error"


class ConfigError(BaseError):
    _default_code = 500
    _default_subcode = 2
    _default_msg = "Configuration error"


class BuildInfoError(BaseError):
    _default_code = 500
    _default_subcode = 3
    _default_msg = "Build info unavailable or corrupted"


class LowDiskSpace(BaseError):
    _default_code = 500
    _default_subcode = 4
    _default_msg = "Critical server error! server reports low or insufficient disk space. please resolve immediately by allocating additional disk space or freeing up storage space."


class TransactionError(BaseError):
    _default_code = 500
    _default_subcode = 10
    _default_msg = "A transaction call has returned with an error"


class DataError(BaseError):
    _default_code = 500
    _default_subcode = 100
    _default_msg = "General data error"


class InconsistentData(BaseError):
    _default_code = 500
    _default_subcode = 101
    _default_msg = "Inconsistent data encountered in document"


class DatabaseUnavailable(BaseError):
    _default_code = 500
    _default_subcode = 102
    _default_msg = "Database is temporarily unavailable"


class UpdateFailed(BaseError):
    _default_code = 500
    _default_subcode = 110
    _default_msg = "Update failed"


class MissingIndex(BaseError):
    _default_code = 500
    _default_subcode = 201
    _default_msg = "Missing internal index"


class NotImplemented(BaseError):
    _default_code = 500
    _default_subcode = 9999
    _default_msg = "Action is not yet implemented"
