"""The address a client used to reach the server, as the reverse proxy passes it on.

Everything is reached through the proxy on port 80, under one path (/cairnml): the web app at /cairnml/, the API under
/cairnml/api and the identity provider under /cairnml/idp. The URLs the server hands out (the sign-in redirects, the
issuer, the storage endpoint) name the host the client typed, so nothing has to be configured per machine. A value set
in the configuration wins.
"""

import contextvars
from urllib.parse import urlencode, urlsplit

from apiserver.config_repo import config


_origin = contextvars.ContextVar("public_origin", default=None)


def remember(headers, host: str) -> None:
    """Called for every request: the proxy's X-Forwarded-* headers name the address the client used."""
    scheme = headers.get("X-Forwarded-Proto") or "http"
    netloc = headers.get("X-Forwarded-Host") or host
    _origin.set(f"{scheme}://{netloc}")


def origin() -> str:
    """The address of this request, e.g. http://localhost or http://192.168.1.20. Only valid while a request runs."""
    current = _origin.get()
    if current is None:
        raise RuntimeError("no client address: this is only known while a request is being served")
    return current


def base_path() -> str:
    """The path everything is served under, e.g. /cairnml."""
    return "/" + config.get("apiserver.idp.public_path").strip("/")


def api_url() -> str:
    return config.get("apiserver.idp.public_url") or origin() + base_path() + "/api"


def web_url() -> str:
    return config.get("apiserver.idp.web_url") or origin() + base_path() + "/"


def idp_issuer() -> str:
    """The identity provider's issuer as the client sees it: the realm path of the configured issuer, on the address the
    client used."""
    return origin() + urlsplit(config.get("apiserver.idp.issuer")).path


def idp_logout_url(id_token_hint: str = "") -> str:
    """Where the browser goes to end the identity provider's session, not only CairnML's. Without it the provider still
    knows the person, and the next sign-in is answered at once, so a logout would change nothing. The provider then
    sends the browser to the sign-in, which now asks for the credentials. The ID token of the sign-in names the session
    to end; without it the provider asks the person to confirm the logout first."""
    params = {
        "client_id": config.get("apiserver.idp.web_client_id"),
        "post_logout_redirect_uri": api_url().rstrip("/") + "/v2.8/login.idp_start",
    }
    if id_token_hint:
        params["id_token_hint"] = id_token_hint
    return f"{idp_issuer()}/protocol/openid-connect/logout?{urlencode(params)}"


def storage_endpoint() -> str:
    configured = config.get("apiserver.storage.public_endpoint")
    if configured:
        return configured
    # Object storage is published on its own port: its presigned URLs name the port the client reaches it on.
    address = urlsplit(origin())
    return f"{address.scheme}://{address.hostname}:{config.get('apiserver.storage.public_port')}"
