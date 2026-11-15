"""The requests of the repositories service. A repository is named by one field, ``ref`` (its id, its name, or its address), and
several by ``refs``. The old names ``id``, ``repository`` and ``repositories`` are refused. See
docs/compatibility/repository-identifier.md."""

from jsonmodels import fields

from apiserver.apimodels import ListField
from apiserver.apimodels.base import StrictRequest


class RepositoryRefRequest(StrictRequest):
    ref = fields.StringField()

    def reference(self) -> str:
        """The repository named by ``ref``; the empty string when none is given."""
        return self.ref or ""


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
    project = fields.StringField()

    def references(self) -> list:
        """The repositories named by ``refs``; none when none is given."""
        return list(self.refs) if self.refs else []


class RequirementRequest(StrictRequest):
    tasks = ListField(
        items_types=str
    )  # the schema requires it; jsonmodels never does for a list, a missing one is the empty list, as before
