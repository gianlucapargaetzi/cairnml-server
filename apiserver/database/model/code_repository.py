"""A code repository a run uses: its address, its owner (the user whose run first used it), and the GitHub App that reads
it. A public repository needs none; a credential on a public one only lifts the rate limit. A private one is read through
its credential, always (see services/devices.git_token)."""

from mongoengine import DateTimeField, Document, StringField

from apiserver.database import Database, strict
from apiserver.database.model import DbModelMixin


class CodeRepository(DbModelMixin, Document):
    meta = {
        "db_alias": Database.backend,
        "strict": strict,
        "collection": "code_repository",
        "indexes": ["company", "ref"],
    }

    id = StringField(primary_key=True)
    company = StringField(required=True)
    ref = StringField(required=True)
    """ owner/name of the repository on GitHub """
    owner = StringField(required=True)
    """ the user whose run first used it: the one who may change its credential """
    credential = StringField()
    """ the UserAccess (a GitHub App) that reads it; empty when none is attached """
    created = DateTimeField()
    last_used = DateTimeField()
