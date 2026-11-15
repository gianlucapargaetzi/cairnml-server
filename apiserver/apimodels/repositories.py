"""The requests of the repositories service. A repository is named by one field, ``ref`` (its id, its name, or its address); ``id``
and ``repository`` are the deprecated names the SDK and the web app still send, accepted for one more release. See
docs/compatibility/repository-identifier.md."""

import logging

from jsonmodels import fields

from apiserver.apimodels import ListField
from apiserver.apimodels.base import StrictRequest

log = logging.getLogger(__name__)


class RepositoryRefRequest(StrictRequest):
    ref = fields.StringField()
    id = fields.StringField()  # deprecated alias of ref
    repository = fields.StringField()  # deprecated alias of ref

    def reference(self, action: str) -> str:
        """The repository named: ``ref``, else ``id``, else ``repository``; the empty string when none is given. A deprecated
        name that supplied the value is logged with the action."""
        if self.ref:
            return self.ref
        for alias in ("id", "repository"):
            value = getattr(self, alias)
            if value:
                log.warning("%s: '%s' is deprecated, send 'ref'", action, alias)
                return value
        return ""


class DeleteRequest(RepositoryRefRequest):
    pass


class SetSharedRequest(RepositoryRefRequest):
    shared = fields.BoolField()
    credential = fields.StringField()
    none_needed = fields.BoolField()


class CheckSharingRequest(RepositoryRefRequest):
    credential = fields.StringField()
    none_needed = fields.BoolField()


class LinkRequest(RepositoryRefRequest):
    project = fields.StringField(required=True)


class UnlinkRequest(RepositoryRefRequest):
    project = fields.StringField(required=True)


class AssignRequest(RepositoryRefRequest):
    role = fields.StringField()
    credential = fields.StringField()
    none_needed = fields.BoolField()


class UnassignRequest(RepositoryRefRequest):
    pass


class CheckRequest(RepositoryRefRequest):
    pass


class RefreshRequest(RepositoryRefRequest):
    pass


class GetRunsRequest(RepositoryRefRequest):
    pass


class GetHistoryRequest(RepositoryRefRequest):
    pass


class GetDatasetVersionsRequest(RepositoryRefRequest):
    pass


class GetAllRequest(StrictRequest):
    project = fields.StringField()


class AddRequest(StrictRequest):
    url = fields.StringField(required=True)
    role = fields.StringField(required=True)
    shared = fields.BoolField()
    project = fields.StringField()
    credential = fields.StringField()
    none_needed = fields.BoolField()


class GetDetailsRequest(RepositoryRefRequest):
    entry = fields.StringField()
    commit = fields.StringField()


class GitTokenRequest(RepositoryRefRequest):
    level = fields.StringField(required=True)  # read or read_write: checked by the handler, which says which


class FindRequest(StrictRequest):
    ref = fields.StringField(required=True)


class CloneRequest(StrictRequest):
    ref = fields.StringField(required=True)
    commit = fields.StringField()


class ResolveRequest(StrictRequest):
    refs = ListField(items_types=str)
    repositories = ListField(items_types=str)  # deprecated alias of refs
    project = fields.StringField()

    def references(self, action: str) -> list:
        """The repositories named: ``refs``, else ``repositories``; none when neither is given."""
        if self.refs:
            return list(self.refs)
        if self.repositories:
            log.warning("%s: 'repositories' is deprecated, send 'refs'", action)
            return list(self.repositories)
        return []


class RequirementRequest(StrictRequest):
    tasks = ListField(
        items_types=str
    )  # the schema requires it; jsonmodels never does for a list, a missing one is the empty list, as before
