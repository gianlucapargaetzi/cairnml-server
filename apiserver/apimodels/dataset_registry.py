from jsonmodels.fields import StringField
from jsonmodels.models import Base


class RegisterRequest(Base):
    repository = StringField(required=True)
    """ The dataset repository on GitHub (URL or owner/name) """
    name = StringField()
    """ The registry name; the repository's name if not given """
