"""A piece of stored data (an artifact, dataset file, model or debug image) and where it lives."""

from enum import StrEnum

from mongoengine import DateTimeField, EnumField, IntField, StringField

from apiserver.database import Database, strict
from apiserver.database.model import AttributedDocument


class DatachunkKind(StrEnum):
    artifact = "artifact"
    dataset = "dataset"
    model = "model"
    debug = "debug"


class DatachunkState(StrEnum):
    pending = "pending"
    """ Upload requested; the bytes may or may not be there yet """
    stored = "stored"
    """ The commit checked the object's size and sha256 against the record """


class Datachunk(AttributedDocument):
    meta = {
        "db_alias": Database.backend,
        "strict": strict,
        "indexes": [
            ("company", "task"),
            ("company", "bucket", "key"),
            ("company", "state", "created"),
        ],
    }

    id = StringField(primary_key=True)
    task = StringField(required=True)
    """ The task the data belongs to; the only task whose job token may upload it """

    project = StringField(default="")
    kind = EnumField(DatachunkKind, required=True)
    bucket = StringField(required=True)
    key = StringField(required=True)
    """ Object key inside the bucket, built by the server from project, task and name """

    name = StringField(required=True)
    size = IntField(required=True)
    sha256 = StringField(required=True)
    """ Hex digest, required: the commit refuses an object whose digest differs """

    content_type = StringField(default="")
    state = EnumField(DatachunkState, default=DatachunkState.pending)
    created = DateTimeField(required=True)
    stored = DateTimeField()
