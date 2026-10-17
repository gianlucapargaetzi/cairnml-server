from jsonmodels.fields import IntField, StringField
from jsonmodels.models import Base


class PrepareUploadRequest(Base):
    task = StringField(required=True)
    """ The task the data belongs to. A job token must name its own task. """
    kind = StringField(required=True)
    """ artifact, dataset, model or debug """
    name = StringField(required=True)
    """ Object name within the task, e.g. "weights/model.pt" """
    size = IntField(required=True)
    sha256 = StringField(required=True)
    """ Hex digest of the bytes. Required: the commit compares the stored object with it. """
    content_type = StringField(default="")


class CommitRequest(Base):
    id = StringField(required=True)


class PrepareDownloadRequest(Base):
    id = StringField()
    """ Either the datachunk id, or task, kind and name (the newest stored object with that name) """
    task = StringField()
    kind = StringField()
    name = StringField()


class DeleteRequest(Base):
    id = StringField()
    """ Either the datachunk id, or task, kind and name (every record with that name) """
    task = StringField()
    kind = StringField()
    name = StringField()
