"""A dataset of the server's environment: one GitHub repository or one registry, with its owner, its visibility, the
projects it is allocated to, and the credential that reads it. Nothing is copied: the data stays in its repository.
See docs/superpowers/specs/2026-11-05-datasets-access-design.md."""

from mongoengine import DateTimeField, Document, ListField, StringField

from apiserver.database import Database, strict
from apiserver.database.model import DbModelMixin

VISIBILITIES = ("user", "projects", "Public")
KINDS = ("repo", "registry", "registry_entry")
STATES = ("ok", "no_access")


class Dataset(DbModelMixin, Document):
    meta = {
        "db_alias": Database.backend,
        "strict": strict,
        "collection": "dataset",
        "indexes": ["company", "ref"],
    }

    id = StringField(primary_key=True)
    company = StringField(required=True)
    kind = StringField(required=True, choices=KINDS)
    ref = StringField(required=True)
    """ owner/name of the repository, or of the registry repository """
    name = StringField(required=True)
    """ the name shown: the repository name, or the registry name """
    owner = StringField(required=True)
    """ the user who added it: the only one who may change its visibility, allocate it or delete it """
    visibility = StringField(default="user", choices=VISIBILITIES)
    projects = ListField(StringField(), default=list)
    """ root projects the dataset is allocated to; used when visibility is 'projects' """
    credential = StringField()
    """ the UserAccess that reads it; empty when the repository is public """
    state = StringField(default="ok", choices=STATES)
    state_message = StringField(default="")
    created = DateTimeField()
    updated = DateTimeField()
    last_checked = DateTimeField()
