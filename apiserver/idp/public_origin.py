"""The address a client used to reach the server, as the reverse proxy passes it on.

Everything is reached through the proxy on port 80, under one path (/cairnml): the web app at /cairnml/, the API under
/cairnml/api and the identity provider under /cairnml/idp. The URLs the server hands out (the sign-in redirects, the
issuer, the storage endpoint) name the host the client typed, so nothing has to be configured per machine.

A deployment can pin its address with apiserver.public_origin (scheme, host and port). Then the server uses that
address for everything security-relevant and ignores the Host and X-Forwarded-* headers of the request; a request that
names another host is logged.

The X-Forwarded-* headers are believed only from the proxies in apiserver.idp.trusted_proxies (by default loopback and
the Docker network the stack's proxy sits on). A request from any other address keeps its own Host and scheme.
"""

import contextvars
import ipaddress
from typing import Callable, List, Optional
from urllib.parse import urlencode, urlsplit

from apiserver.config_repo import config

log = config.logger(__file__)

_origin = contextvars.ContextVar("public_origin", default=None)


def pinned_origin() -> str:
    """The address the deployment pinned, e.g. https://cairnml.example, or empty when the address is not pinned."""
    return (config.get("apiserver.idp.public_origin") or "").rstrip("/")


def is_trusted_proxy(remote_addr: Optional[str], get: Optional[Callable] = None) -> bool:
    """True when the request comes from one of apiserver.idp.trusted_proxies. An unknown address is not trusted."""
    if not remote_addr:
        return False
    try:
        address = ipaddress.ip_address(str(remote_addr).split("%")[0])
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    return any(address in network for network in _trusted_networks(get))


def _trusted_networks(get: Optional[Callable] = None) -> list:
    get = get or config.get
    return [ipaddress.ip_network(str(entry), strict=False) for entry in get("apiserver.idp.trusted_proxies") or []]


def remember(headers, host: str, remote_addr: Optional[str] = None) -> None:
    """Called for every request: the X-Forwarded-* headers name the address the client used, when a trusted proxy
    passed them on. From any other address they are ignored, and the Host of the request is used."""
    trusted = is_trusted_proxy(remote_addr)
    forwarded = headers.get("X-Forwarded-Host") or headers.get("X-Forwarded-Proto")
    if forwarded and not trusted:
        log.warning("forwarded headers from %s ignored: not a trusted proxy (apiserver.idp.trusted_proxies)", remote_addr)
    if trusted:
        scheme = headers.get("X-Forwarded-Proto") or "http"
        netloc = headers.get("X-Forwarded-Host") or host
    else:
        scheme, netloc = "http", host
    pinned = pinned_origin()
    if pinned and trusted and headers.get("X-Forwarded-Host") and netloc.lower() != urlsplit(pinned).netloc.lower():
        log.warning("request for host %s ignored: the server is pinned to %s", netloc, pinned)
    _origin.set(f"{scheme}://{netloc}")


def origin() -> str:
    """The address of this request, e.g. http://localhost or http://192.168.1.20, or the pinned address. Only valid
    while a request runs."""
    pinned = pinned_origin()
    if pinned:
        return pinned
    current = _origin.get()
    if current is None:
        raise RuntimeError("no client address: this is only known while a request is being served")
    return current


def public_origin_problems(get: Callable = config.get) -> List[str]:
    """Refuses a pinned address that cannot be used: it must be scheme://host[:port], with no path."""
    pinned = (get("apiserver.idp.public_origin") or "").rstrip("/")
    if not pinned:
        return []
    problem = [f"apiserver.public_origin must be scheme://host[:port], e.g. https://cairnml.example (got {pinned!r})"]
    try:
        address = urlsplit(pinned)
        address.port  # raises ValueError for a port that is not a number
    except ValueError:
        return problem
    if address.scheme not in ("http", "https") or not address.hostname or address.path or address.query:
        return problem
    return []


def trusted_proxies_problems(get: Callable = config.get) -> List[str]:
    """Refuses an entry of apiserver.idp.trusted_proxies that is not an address or a network."""
    problems = []
    for entry in get("apiserver.idp.trusted_proxies") or []:
        try:
            ipaddress.ip_network(str(entry), strict=False)
        except ValueError:
            problems.append(f"apiserver.idp.trusted_proxies: {entry!r} is not an address or network, e.g. 10.0.0.0/8")
    return problems


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
