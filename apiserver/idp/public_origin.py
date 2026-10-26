"""The address a client used to reach the server, as the reverse proxy passes it on.

Everything is reached through the proxy on port 80: the web app at /, the API under /api and the identity provider under
/idp. The URLs the server hands out (the sign-in redirects, the issuer, the storage endpoint) name the host the client
typed, so nothing has to be configured per machine. A value set in the configuration wins.
"""

import contextvars
from urllib.parse import urlsplit

from apiserver.config_repo import config

# Object storage is published on its own port: its presigned URLs name the port the client reaches it on.
STORAGE_PORT = 9000

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


def api_url() -> str:
    return config.get("apiserver.idp.public_url") or origin() + "/api"


def web_url() -> str:
    return config.get("apiserver.idp.web_url") or origin() + "/"


def idp_issuer() -> str:
    """The identity provider's issuer as the client sees it: the realm path of the configured issuer, on the address the
    client used."""
    return origin() + urlsplit(config.get("apiserver.idp.issuer")).path


def storage_endpoint() -> str:
    configured = config.get("apiserver.storage.public_endpoint")
    if configured:
        return configured
    address = urlsplit(origin())
    return f"{address.scheme}://{address.hostname}:{STORAGE_PORT}"
