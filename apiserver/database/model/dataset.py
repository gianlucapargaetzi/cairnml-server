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
    reference = StringField(default="")
    """ the name a run uses to name this dataset, unique in the company: the repository name, or <registry>/<name> for an
    entry of a registry. Empty for a dataset kept before references existed """
    owner = StringField(required=True)
    """ the user who added it: the only one who may change its visibility, allocate it or delete it """
    visibility = StringField(default="user", choices=VISIBILITIES)
    credential = StringField()
    """ the UserAccess that reads it; empty when the repository is public """
    source = StringField(default="")
    """ for a registry entry: how its data is read (the entry's source field) """
    link = StringField(default="")
    """ for a registry entry: where its data is (the entry's link field) """
    metadata = StringField(default="")
    """ for a registry entry: its metadata file as JSON text, read with the registry (empty when there is no file). Kept
    as text because label names are arbitrary keys, which Mongo does not take as field names """
    read_at = DateTimeField()
    """ when the registry (or, for an entry, the registry read that stored it) was last read from GitHub """
    state = StringField(default="ok", choices=STATES)
    state_message = StringField(default="")
    created = DateTimeField()
    updated = DateTimeField()
    last_checked = DateTimeField()
