from jsonmodels.fields import StringField
from jsonmodels.models import Base


class RegistryRequest(Base):
    repository = StringField(required=True)
    """ owner/name of a GitHub repository, or its github.com address """
