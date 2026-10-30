from apiserver.apimodels.login import (
    GetSupportedModesRequest,
    GetSupportedModesResponse,
    BasicMode,
    BasicGuestMode,
    ServerErrors,
)
from apiserver.config import info
from apiserver.service_repo import endpoint, APICall
from apiserver.service_repo.auth import revoke_auth_token


@endpoint("login.supported_modes", response_data_model=GetSupportedModesResponse)
def supported_modes(call: APICall, _, __: GetSupportedModesRequest):
    # Username and password sign-in is removed; the identity provider is the only way in.
    return GetSupportedModesResponse(
        basic=BasicMode(enabled=False, guest=BasicGuestMode()),
        sso={},
        sso_providers=[],
        server_errors=ServerErrors(
            missed_es_upgrade=info.missed_es_upgrade,
            es_connection_error=info.es_connection_error,
        ),
        authenticated=call.auth is not None,
    )


@endpoint("login.logout", min_version="2.13")
def logout(call: APICall, _, __):
    from http.cookies import CookieError, SimpleCookie

    from apiserver.idp import public_origin
    from apiserver.idp.web_login import ID_TOKEN_COOKIE

    revoke_auth_token(call.auth)
    call.result.set_auth_cookie(None)
    # The ID token of this browser's sign-in names the identity provider's session to end.
    try:
        hint = SimpleCookie(call.headers.get("Cookie", "")).get(ID_TOKEN_COOKIE)
    except CookieError:
        hint = None
    call.result.cookies[ID_TOKEN_COOKIE] = None
    # The web app sends the browser here: it ends the identity provider's session too, then comes back to the sign-in.
    return {"redirect_url": public_origin.idp_logout_url(hint.value if hint else "")}


# ---- web sign-in with the identity provider ----
import requests as _requests  # noqa: E402

from apiserver.apimodels.devices import IdpCallbackRequest  # noqa: E402
from apiserver.apierrors import errors as _errors  # noqa: E402
from apiserver.config_repo import config as _config  # noqa: E402
from apiserver.idp import public_origin as _public_origin  # noqa: E402
from apiserver.idp.audit import client_address, record as _audit  # noqa: E402
from apiserver.idp.tokens import issue_access_token as _issue  # noqa: E402
from apiserver.idp.verifier import get_verifier as _verifier  # noqa: E402
from apiserver.idp.web_login import (  # noqa: E402
    ID_TOKEN_COOKIE as _ID_TOKEN_COOKIE,
    LoginRefused as _LoginRefused,
    finish_login as _finish,
    start_login as _start,
)
from apiserver.redis_manager import redman as _redman  # noqa: E402

_login_store = _redman.connection("apiserver")
_idp_http = _requests.Session()


def _redirect_uri() -> str:
    return f"{_public_origin.api_url().rstrip('/')}/v2.8/login.idp_callback"


@endpoint("login.idp_start", min_version="2.8")
def idp_start(call: APICall, _company, _):
    # The browser signs in at the identity provider's address as the browser reaches it (through the proxy).
    call.result.redirect = _start(_login_store, _public_origin.idp_issuer(),
                                  _config.get("apiserver.idp.web_client_id"), _redirect_uri())


@endpoint("login.idp_callback", min_version="2.8", request_data_model=IdpCallbackRequest)
def idp_callback(call: APICall, _company, request: IdpCallbackRequest):
    issuer = _config.get("apiserver.idp.issuer")
    client = _config.get("apiserver.idp.web_client_id")
    try:
        user, identity = _finish(
            _login_store, _idp_http, _verifier(), issuer, client, _redirect_uri(),
            request.state, request.code, _config.get("apiserver.idp.admin_role"),
        )
    except _LoginRefused:
        _audit("anonymous", "web.login_refused", ip=client_address(call))
        raise _errors.unauthorized.InvalidToken("sign-in refused") from None
    token = _issue(user.id, user.company, "", kind="web", role=user.role, user_name=user.name)
    call.result.set_auth_cookie(token)
    call.result.cookies[_ID_TOKEN_COOKIE] = identity.id_token
    _audit(user.id, "web.login", ip=client_address(call), company=user.company)
    call.result.redirect = _public_origin.web_url()
