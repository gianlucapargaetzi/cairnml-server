"""A Git credential for one host (and optionally a path prefix on it), encrypted with the server's key.
The secret is never stored in clear and never returned by the API: see apiserver/idp/git_credentials.py."""

from mongoengine import DateTimeField, Document, StringField

from apiserver.database import Database, strict
from apiserver.database.model import DbModelMixin


class GitCredential(DbModelMixin, Document):
    meta = {
        "db_alias": Database.backend,
        "strict": strict,
        "indexes": [{"fields": ["company", "host", "path"], "unique": True}],
    }

    id = StringField(primary_key=True)
    company = StringField(required=True)
    host = StringField(required=True)
    """ The host, lower case, with the port if there is one """

    path = StringField(default="")
    """ A path prefix on the host, lower case, without slashes at either end; empty for the whole host """

    username = StringField(required=True)
    secret = StringField(required=True)
    """ The secret encrypted with the key from the secrets directory (a Fernet token) """

    created = DateTimeField()
    updated = DateTimeField()
