"""A repository that no access reaches: no App is installed on it, no token is shared with the project, and it is not
public. A job that needs such a repository is not refused at submission: it is listed as Failed, with this reason."""

from apiserver.apierrors import errors


class AccessRefused(errors.bad_request.ValidationError):
    pass
