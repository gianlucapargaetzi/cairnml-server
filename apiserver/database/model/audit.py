"""An audit record: who did what, from where, and the details of the action."""

from datetime import datetime

from mongoengine import DateTimeField, DictField, Document, StringField

from apiserver.database import Database, strict
from apiserver.database.model import DbModelMixin


class AuditEvent(DbModelMixin, Document):
    meta = {"db_alias": Database.auth, "strict": strict, "indexes": ["action", "ts"]}

    ts = DateTimeField(default=datetime.utcnow)
    actor = StringField(required=True, max_length=64)
    action = StringField(required=True, max_length=64)
    detail = DictField(default=dict)
    ip = StringField(max_length=64)
