from jsonmodels.fields import StringField
from jsonmodels.models import Base


class RegistryRequest(Base):
    repository = StringField(required=True)
    """ owner/name of a GitHub repository, or its github.com address """
    visibility = StringField()
    """ user (the default), projects or Public, as for a dataset (bll/datasets) """
