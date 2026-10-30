"""Admin endpoints for the Git credentials of hosts other than GitHub. Write-only: there is no endpoint that returns a
secret. The roles that may call them are set in apiserver/schema/services/git_credentials.conf."""

from apiserver.apimodels.git_credentials import DeleteGitCredentialRequest, SetGitCredentialRequest
from apiserver.idp import git_credentials as store
from apiserver.service_repo import APICall, endpoint


@endpoint("git_credentials.set", min_version="2.8", request_data_model=SetGitCredentialRequest)
def set_credential(call: APICall, company: str, request: SetGitCredentialRequest):
    call.result.data = {
        "credential": store.set_credential(
            company, request.host, request.path, request.username, request.secret, actor=call.identity.user)
    }


@endpoint("git_credentials.delete", min_version="2.8", request_data_model=DeleteGitCredentialRequest)
def delete_credential(call: APICall, company: str, request: DeleteGitCredentialRequest):
    call.result.data = {
        "deleted": store.delete_credential(company, request.host, request.path, actor=call.identity.user)
    }


@endpoint("git_credentials.get_all", min_version="2.8")
def get_all(call: APICall, company: str, _):
    call.result.data = {"credentials": store.list_credentials(company)}
