"""The requests of the user_access service (the repository credentials a user owns). ``id`` here is the id of an access record,
not of a repository, and stays ``id``."""
from jsonmodels import fields

from apiserver.apimodels.base import StrictRequest


class GetAllRequest(StrictRequest):
    pass


class AddAppRequest(StrictRequest):
    label = fields.StringField(required=True)
    app_id = fields.StringField(required=True)
    api_url = fields.StringField()
    key = fields.StringField(required=True)
    scope = fields.StringField()  # user (the default) or company


class AccessIdRequest(StrictRequest):
    id = fields.StringField(required=True)
