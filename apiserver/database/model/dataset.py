"""A dataset of the server's environment: one GitHub repository or one registry, with its owner, its visibility, and the
credential that reads it. The projects it is allocated to are its project shares (bll/access). Nothing is copied: the data stays in its repository.
See docs/superpowers/specs/2026-11-05-datasets-access-design.md."""

from mongoengine import DateTimeField, Document, StringField

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
    credential = StringField()
    """ the UserAccess that reads it; empty when the repository is public """
    source = StringField(default="")
    """ for a registry entry: how its data is read (the entry's source field) """
    link = StringField(default="")
    """ for a registry entry: where its data is (the entry's link field) """
    state = StringField(default="ok", choices=STATES)
    state_message = StringField(default="")
    created = DateTimeField()
    updated = DateTimeField()
    last_checked = DateTimeField()
