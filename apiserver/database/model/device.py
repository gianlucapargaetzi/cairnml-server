"""A laptop or agent that is allowed to get access tokens, identified by its public key."""

from mongoengine import BooleanField, DateTimeField, Document, StringField

from apiserver.database import Database, strict
from apiserver.database.model import DbModelMixin
from apiserver.database.utils import get_options


class DeviceKind(object):
    client = "client"
    """ A person's laptop or workstation running the SDK """
    agent = "agent"
    """ A machine running clearml-agent """


class Device(DbModelMixin, Document):
    meta = {"db_alias": Database.auth, "strict": strict, "indexes": ["user", "company"]}

    id = StringField(primary_key=True)
    user = StringField(required=True)
    """ The person the device belongs to (the user's id) """

    company = StringField(required=True)
    kind = StringField(required=True, choices=get_options(DeviceKind), default=DeviceKind.client)
    name = StringField(default="")
    public_key = StringField(required=True)
    """ PEM of the device's public key; the private key never leaves the device """

    created = DateTimeField()
    last_used = DateTimeField()
    revoked = BooleanField(default=False)
    """ A revoked device is refused on its next request """
