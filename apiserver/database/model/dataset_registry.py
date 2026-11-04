from mongoengine import Document, StringField, DateTimeField

from apiserver.database import Database, strict
from apiserver.database.model import DbModelMixin


class DatasetRegistry(DbModelMixin, Document):
    """
    A public dataset registry of the server: a GitHub repository with one file per entry (see bll/dataset_registry).
    The list is server-wide, not per company. Changed by admins only.
    """

    meta = {
        "db_alias": Database.backend,
        "strict": strict,
        "collection": "dataset_registry",
    }

    id = StringField(primary_key=True)
    """ The repository in lower case, so that the same repository is not added twice """
    repository = StringField(required=True)
    """ The repository as owner/name """
    url = StringField()
    """ The https address of the repository, as it was given (github.com only) """
    added = DateTimeField(required=True)
    added_by = StringField(required=True)
