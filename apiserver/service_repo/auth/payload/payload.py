from ..identity import Identity
from ..dictable import Dictable


class Payload(Dictable):
    def __init__(self, auth_type, identity=None, entities=None):
        self._auth_type = auth_type
        self.identity = identity
        self.entities = entities or {}

    @property
    def auth_type(self):
        return self._auth_type

    @property
    def identity(self):
        return self._identity

    @identity.setter
    def identity(self, value):
        if isinstance(value, dict):
            value = Identity(**value)
        else:
            assert isinstance(value, Identity)
        self._identity = value

    @property
    def entities(self):
        return self._entities

    @entities.setter
    def entities(self, value):
        self._entities = value

    def get_log_entry(self):
        return {
            "type": self.auth_type,
            "identity": self.identity.to_dict(),
            "entities": self.entities,
        }
