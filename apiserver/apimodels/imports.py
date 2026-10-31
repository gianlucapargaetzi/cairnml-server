from jsonmodels.fields import IntField, StringField
from jsonmodels.models import Base

from apiserver.apimodels import ListField


class PrepareUploadRequest(Base):
    name = StringField(required=True)
    """ The ZIP's file name, for example "run.zip" """
    size = IntField(required=True)
    sha256 = StringField(required=True)
    """ Hex digest of the ZIP. The commit compares the stored object with it. """
    content_type = StringField(default="application/zip")


class PreparePartRequest(Base):
    id = StringField(required=True)
    part = IntField(required=True)
    """ 1-based part number """


class CommitRequest(Base):
    id = StringField(required=True)
    parts = ListField([dict])
    """ Multipart uploads only: [{number, etag}] for every part """


class RunRequest(Base):
    id = StringField(required=True)
    """ The id of the stored staging upload """
