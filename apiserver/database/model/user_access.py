"""A repository access owned by one user: a token for a host (and a path prefix on it), or a GitHub App with its ID, its
key and its API address. The secret is encrypted with the server's key and is readable only by its owner (through the
server's own checks), never returned by the API."""

from mongoengine import DateTimeField, Document, ListField, StringField

from apiserver.database import Database, strict
from apiserver.database.model import DbModelMixin


class UserAccess(DbModelMixin, Document):
    meta = {
        "db_alias": Database.backend,
        "strict": strict,
        "indexes": ["user", "company"],
    }

    id = StringField(primary_key=True)
    company = StringField(required=True)
    user = StringField(required=True)
    """ The owner: the only user who may read, check, change or delete the access """

    kind = StringField(required=True, choices=("token", "app"))
    label = StringField(required=True)
    host = StringField(default="github.com")
    path = StringField(default="")
    app_id = StringField()
    api_url = StringField()
    secret = StringField(required=True)
    """ The token, or the App's private key, encrypted (a Fernet token) """

    created = DateTimeField()
    updated = DateTimeField()
    last_checked = DateTimeField()
    last_state = StringField()
    last_message = StringField()
    granted_projects = ListField(StringField(), default=list)
    """ Root projects that may use this access. Their members can use it, never read the secret; a grant stops at once
    when the owner leaves the project or is deactivated, or when the user using it leaves the project """
