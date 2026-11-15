from mongoengine import EmbeddedDocument, StringField

from apiserver.database.model.base import ProperDictMixin


class MetadataItem(EmbeddedDocument, ProperDictMixin):
    key = StringField(required=True)
    type = StringField(required=True)
    value = StringField(required=True)
