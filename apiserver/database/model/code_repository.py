"""The code repositories runs use, in two parts.

A source is the repository itself: one per company and repository, whatever the spelling of its address (see
idp/repo_address), with what the server learned about reaching it without a credential.

A binding is how one root project reads that source: the project's owner owns it, the first user who ran it there
discovered it, and a credential (or "none needed") is assigned to it. A run in a project without a root project binds to
its user (scope ``user:<id>``).
"""

from mongoengine import BooleanField, DateTimeField, Document, StringField

from apiserver.database import Database, strict
from apiserver.database.model import DbModelMixin


class CodeRepository(DbModelMixin, Document):
    meta = {
        "db_alias": Database.backend,
        "strict": strict,
        "collection": "repository_source",
        "indexes": [{"fields": ["company", "key"], "unique": True}],
    }

    id = StringField(primary_key=True)
    company = StringField(required=True)
    key = StringField(required=True)
    """ host/path in the form that is the same for every spelling of the address """
    host = StringField(required=True)
    path = StringField(required=True)
    """ the path as it was first written """
    anonymous = BooleanField()
    """ whether the repository can be cloned without a credential; unset until asked """
    anonymous_checked = DateTimeField()
    created = DateTimeField()
    last_used = DateTimeField()


class RepositoryBinding(DbModelMixin, Document):
    meta = {
        "db_alias": Database.backend,
        "strict": strict,
        "collection": "repository_binding",
        "indexes": [{"fields": ["source", "scope"], "unique": True}, "company"],
    }

    id = StringField(primary_key=True)
    company = StringField(required=True)
    source = StringField(required=True)
    scope = StringField(required=True)
    """ the id of the root project, or ``user:<id>`` for a run outside any project """
    project = StringField()
    """ the root project, empty for a run outside any project """
    owner = StringField(required=True)
    """ the owner of the root project (the run's user outside any project): the one who assigns the credential """
    discovered_by = StringField(required=True)
    """ the user whose run first used the repository in this scope """
    credential = StringField()
    """ the UserAccess that reads the repository for this scope; empty when none is assigned """
    none_needed = BooleanField(default=False)
    """ assigned as "None needed": the repository is cloned without a credential """
    created = DateTimeField()
    last_used = DateTimeField()
