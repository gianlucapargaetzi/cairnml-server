from jsonmodels import models, fields
from jsonmodels.validators import Length

from apiserver.apimodels import MongoengineFieldsDict, ListField


class UpdateResponse(models.Base):
    updated = fields.IntField(required=True)
    fields = MongoengineFieldsDict()


class PagedRequest(models.Base):
    page = fields.IntField()
    page_size = fields.IntField()


class IdResponse(models.Base):
    id = fields.StringField(required=True)


class MakePublicRequest(models.Base):
    ids = ListField(items_types=str, validators=[Length(minimum_value=1)])


class MoveRequest(models.Base):
    ids = ListField([str], validators=Length(minimum_value=1))
    project = fields.StringField()
    project_name = fields.StringField()


class StrictRequest(models.Base):
    """A request that refuses a field it does not declare. ``jsonmodels`` drops unknown fields without a word, so a misspelt or
    stale name would be taken for an omitted one; here it is a ``TypeError``, which the call machinery reports as a validation
    error (see ``APICall``)."""

    def __init__(self, **kwargs):
        known = {attr_name for attr_name, _, _ in self.iterate_with_name()}
        unknown = sorted(set(kwargs) - known)
        if unknown:
            raise TypeError(f"unknown field: {', '.join(unknown)}")
        super().__init__(**kwargs)
