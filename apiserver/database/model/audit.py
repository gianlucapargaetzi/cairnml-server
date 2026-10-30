"""An audit record: who did what, from where, and the details of the action."""

from datetime import datetime

from mongoengine import DateTimeField, DictField, Document, StringField

from apiserver.database import Database, strict
from apiserver.database.model import DbModelMixin

# Events older than this are removed by MongoDB (a TTL index on ts). The migration 2_5_0 creates the same index on
# databases that already have events.
RETENTION_DAYS = 365
RETENTION_SECONDS = RETENTION_DAYS * 24 * 60 * 60


class AuditEvent(DbModelMixin, Document):
    meta = {
        "db_alias": Database.auth,
        "strict": strict,
        "indexes": ["action", "company", {"fields": ["ts"], "expireAfterSeconds": RETENTION_SECONDS}],
    }

    ts = DateTimeField(default=datetime.utcnow)
    actor = StringField(required=True, max_length=64)
    action = StringField(required=True, max_length=64)
    detail = DictField(default=dict)
    ip = StringField(max_length=64)
    company = StringField(max_length=64)
