"""The check made when a job on a Git host other than GitHub is submitted. A private repository needs Git
credentials stored for its host (or path prefix); without them the job is refused at once, naming the host.
A public repository is accepted. A repository whose host cannot be asked, or whose address is refused by the
probe rules below, is not known to be public, so it is refused without credentials too.

The host is asked anonymously with the smart HTTP discovery request that every Git host serves. A host that
answers with the upload-pack advertisement allows anonymous reads; one that refuses (401, 403, 404, or a sign-in
page) does not. Redirects are not followed.

The probe is made only to public addresses: the name is resolved first and every resolved address is checked,
so a name that points at loopback, link-local, private or unique-local space (or at a configured network, such as
the server's own Docker network) is never contacted."""

import ipaddress
import logging
import socket
from typing import Optional
from urllib.parse import urlsplit

import requests

from apiserver.apierrors import errors
from apiserver.config_repo import config
from apiserver.idp import git_credentials as store

log = logging.getLogger("cairnml.idp.repo_visibility")
PROBE_TIMEOUT_SEC = 10
ADVERTISEMENT = "application/x-git-upload-pack-advertisement"

# Always refused, whatever the configuration: loopback, link-local (including the cloud metadata address),
# private (RFC1918) and unique-local (fc00::/7), and the IPv6 equivalents. ipaddress also reports them through
# is_private, but they are listed so the rule is visible here.
ALWAYS_BLOCKED = tuple(ipaddress.ip_network(net) for net in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.168.0.0/16", "224.0.0.0/4", "240.0.0.0/4", "::/128", "::1/128", "fc00::/7", "fe80::/10",
    "ff00::/8",
))


class ProbeBlocked(Exception):
    """A resolved address is not one the probe may contact. The message names the address, not the host's data."""


class ProbeFailed(Exception):
    """The host name does not resolve."""


def blocked_networks() -> tuple:
    """The always-refused ranges plus the networks configured for this deployment."""
    extra = []
    for value in config.get("apiserver.idp.repo_probe_blocked_networks", []) or []:
        extra.append(ipaddress.ip_network(value, strict=False))
    return ALWAYS_BLOCKED + tuple(extra)


def _address_blocked(address: str, networks: tuple) -> bool:
    ip = ipaddress.ip_address(address)
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_multicast or ip.is_unspecified:
        return True
    return any(ip in network for network in networks)


def _probe_url(repository: str) -> Optional[tuple]:
    """(host, port, URL of the discovery request) for an HTTP(S) repository URL, or None for anything else."""
    if "://" not in repository and ":" in repository.split("/", 1)[0]:
        return None  # the scp-like form git@host:path, which is an SSH address
    parts = urlsplit(repository if "://" in repository else f"https://{repository}")
    if parts.scheme not in ("http", "https"):
        return None
    host = parts.netloc.rsplit("@", 1)[-1]
    path = parts.path.strip("/")
    if not host or not path:
        return None
    if path.endswith(".git"):
        path = path[:-4]
    try:
        port = parts.port
    except ValueError:
        return None
    name = (parts.hostname or "").lower()
    return name, port or (443 if parts.scheme == "https" else 80), \
        f"{parts.scheme}://{host}/{path}/info/refs?service=git-upload-pack"


def check_address(name: str, port: int) -> None:
    """Resolve the host and raise ProbeBlocked when any address it resolves to is not allowed.
    Raises ProbeFailed when the name does not resolve."""
    try:
        infos = socket.getaddrinfo(name, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        raise ProbeFailed(name) from None
    networks = blocked_networks()
    for info in infos:
        address = info[4][0]
        if _address_blocked(address, networks):
            raise ProbeBlocked(f"{name} resolves to {address}, which the server does not contact")


def is_private(repository: str) -> Optional[bool]:
    """True when the host refuses an anonymous read, False when it allows one, None when it cannot be asked
    (no name, a refused address, a name that does not resolve, or a host that fails)."""
    probe = _probe_url(repository)
    if probe is None:
        return None
    name, port, url = probe
    try:
        check_address(name, port)
    except (ProbeBlocked, ProbeFailed) as exc:
        log.info("not probing %s: %s", name, exc.__class__.__name__)
        return None
    try:
        response = requests.get(url, timeout=PROBE_TIMEOUT_SEC, allow_redirects=False)
    except requests.RequestException:
        log.info("could not ask %s whether a repository is public", name)
        return None
    if response.status_code >= 500 or 300 <= response.status_code < 400:
        return None
    public = response.status_code == 200 and response.headers.get("Content-Type", "").startswith(ADVERTISEMENT)
    return not public


def check_readable(company: str, repository: str) -> None:
    """Raise a validation error unless the repository is known to be public or Git credentials are stored for it.
    A private repository, or one whose host cannot be asked, needs credentials."""
    if is_private(repository) is False:
        return
    if store.find_credential(company, repository) is not None:
        return
    host, _path = store.host_and_path(repository)
    raise errors.bad_request.ValidationError(
        f"the repository on {host} is private or cannot be checked, and no Git credentials are stored for {host}: "
        "an admin adds them with the git_credentials.set call (the web app's Settings page), or the repository "
        "must be public and reachable from the server")
