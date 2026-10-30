from jsonmodels.fields import StringField
from jsonmodels.models import Base


class EnrollRequest(Base):
    kind = StringField(default="client")
    """ 'client' for a person's laptop, 'agent' for a machine that runs jobs """
    name = StringField(default="")
    public_key = StringField(required=True)
    """ PEM of the device's public key """


class ExchangeRequest(Base):
    assertion = StringField(required=True)
    """ Short JWT signed with the device's private key """


class DeviceListRequest(Base):
    pass


class RevokeRequest(Base):
    id = StringField(required=True)


class ReassignRequest(Base):
    id = StringField(required=True)
    """ The device to move """
    user = StringField(required=True)
    """ The id of the admin who becomes the device's owner """


class IdpCallbackRequest(Base):
    state = StringField(required=True)
    code = StringField(required=True)


class JobTokenRequest(Base):
    task = StringField(required=True)
    """ The task the agent has taken from its queue """


class GitTokenRequest(Base):
    task = StringField(required=True)
    """ The task whose repository the caller needs to clone """
    repository = StringField()
    """ A dataset repository to clone instead of the task's own. Must be allowed in the server's configuration. """
