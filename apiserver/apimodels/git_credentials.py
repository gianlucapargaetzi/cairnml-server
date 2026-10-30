from jsonmodels.fields import StringField
from jsonmodels.models import Base


class SetGitCredentialRequest(Base):
    host = StringField(required=True)
    """ The host, such as gitlab.example.com (a port may follow) """
    path = StringField(default="")
    """ A path prefix on the host, such as team/ . Empty for every repository on the host """
    username = StringField(required=True)
    secret = StringField(required=True)
    """ The password or access token. Write-only: it is never returned """


class DeleteGitCredentialRequest(Base):
    host = StringField(required=True)
    path = StringField(default="")
