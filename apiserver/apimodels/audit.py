from jsonmodels.fields import IntField, StringField
from jsonmodels.models import Base


class AuditListRequest(Base):
    actor = StringField()
    """ Only the events of this actor (a user id or a device id) """
    action = StringField()
    """ Only events of this action, such as 'token.refused' """
    since = StringField()
    """ Only events at or after this time (ISO 8601) """
    until = StringField()
    """ Only events at or before this time (ISO 8601) """
    page = IntField(default=0)
    page_size = IntField(default=50)
