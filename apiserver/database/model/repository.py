"""The repositories of the environment: code, datasets and registries of datasets are one kind of record.

A repository is a git repository and how to read it. Its roles say how it is used (code that a run checks out, a dataset
that the SDK clones, a registry that lists datasets). A registry has entries. Who reads it with which credential is the
assignments of the users (never of a project). Where it shows up is the project links, which are shares of the
repository (apiserver/database/model/share.py); a shared entry, owned by the admin, has a public share.
See docs/superpowers/specs/2026-11-11-unified-repositories-design.md.
"""

from mongoengine import BooleanField, DateTimeField, Document, ListField, StringField

from apiserver.database import Database, strict
from apiserver.database.model import DbModelMixin

ROLES = ("code", "dataset", "registry")
STATES = ("ok", "no_access")


class Repository(DbModelMixin, Document):
    meta = {
        "db_alias": Database.backend,
        "strict": strict,
        "collection": "repository",
        "indexes": [{"fields": ["company", "key"], "unique": True}, "company"],
    }

    id = StringField(primary_key=True)
    company = StringField(required=True)
    key = StringField(required=True)
    """ host/path in the form that is the same for every spelling of the address """
    host = StringField(required=True)
    path = StringField(required=True)
    """ the path as it was first written """
    name = StringField(required=True)
    """ the name shown: the last part of the path """
    reference = StringField(default="")
    """ the name a run uses to name this dataset, unique in the company; empty when the repository is not a dataset """
    roles = ListField(StringField(choices=ROLES))
    owner = StringField(required=True)
    """ the user who owns it; the admin for a shared entry """
    state = StringField(default="ok", choices=STATES)
    state_message = StringField(default="")
    """ what the last check of it found; for a registry, whether its entries could be read """
    anonymous = BooleanField()
    """ whether the repository can be cloned without a credential; unset until asked """
    anonymous_checked = DateTimeField()
    read_at = DateTimeField()
    """ when the entries of a registry were last read """
    created = DateTimeField()
    updated = DateTimeField()
    last_used = DateTimeField()
    last_checked = DateTimeField()


class RegistryEntry(DbModelMixin, Document):
    """A dataset a registry lists: read once with the registry, stored with its metadata. It has no credential of its own."""

    meta = {
        "db_alias": Database.backend,
        "strict": strict,
        "collection": "registry_entry",
        "indexes": [{"fields": ["company", "registry", "name"], "unique": True}],
    }

    id = StringField(primary_key=True)
    company = StringField(required=True)
    registry = StringField(required=True)
    """ the id of the registry repository """
    name = StringField(required=True)
    reference = StringField(default="")
    source = StringField(default="")
    link = StringField(default="")
    metadata = StringField(default="")
    """ the metadata file as JSON text (label names are arbitrary keys, which Mongo does not take as field names) """
    read_at = DateTimeField()
    created = DateTimeField()
    updated = DateTimeField()


class Assignment(DbModelMixin, Document):
    """How one user reads one repository: with a credential of theirs (or the company's), or without ("None needed")."""

    meta = {
        "db_alias": Database.backend,
        "strict": strict,
        "collection": "repository_assignment",
        "indexes": [{"fields": ["repository", "user"], "unique": True}, "company"],
    }

    id = StringField(primary_key=True)
    company = StringField(required=True)
    repository = StringField(required=True)
    user = StringField(required=True)
    credential = StringField()
    none_needed = BooleanField(default=False)
    created = DateTimeField()
    last_used = DateTimeField()
