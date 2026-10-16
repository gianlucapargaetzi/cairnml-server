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


class IdpCallbackRequest(Base):
    state = StringField(required=True)
    code = StringField(required=True)
