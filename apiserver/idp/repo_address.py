"""The identity of a git repository, whatever the spelling of its address.

``https://github.com/Acme/Proj.git``, ``git@github.com:acme/proj``, ``ssh://git@github.com:22/acme/proj.git`` and
``GIT://GitHub.com/ACME/proj/`` all name one repository, so they have one key. The key is the host and the path: the
host in lower case without a user, a port or ``www.``; the path without a trailing ``.git`` or slashes, and in lower case
on the hosts that ignore case (GitHub, GitLab, Bitbucket). The transport (https, ssh, git) and the port are not part of
the identity: they say how to reach the repository, not which one it is.

A pure module: no network, no database.
"""

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

SCHEMES = ("http", "https", "git", "ssh", "git+ssh", "ssh+git")
CASE_INSENSITIVE_HOSTS = ("github.com", "gitlab.com", "bitbucket.org")
""" Hosts whose repository names are case-insensitive: two spellings of the case name one repository there """
GITHUB_HOSTS = ("github.com", "www.github.com")

_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
""" A host name with at least one dot (``localhost`` and a drive letter are not hosts of a remote) """
_SCP = re.compile(r"^(?:[^@/\s:]+@)?([^/\s:@]+):(?!//)(.+)$")
""" ``[user@]host:path``, the form git prints for ssh """


@dataclass(frozen=True)
class RepoAddress:
    host: str
    path: str
    """ the path as it was first written, without ``.git`` and slashes """

    @property
    def key(self) -> str:
        """The identity: the same for every spelling of the address."""
        path = self.path.lower() if self.host in CASE_INSENSITIVE_HOSTS else self.path
        return f"{self.host}/{path}"

    @property
    def url(self) -> str:
        """The https address the repository is cloned from."""
        return f"https://{self.host}/{self.path}"

    @property
    def is_github(self) -> bool:
        return self.host == "github.com"


def _clean_path(path: str) -> str:
    segments = [segment for segment in path.replace("\\", "/").split("/") if segment]
    if not segments:
        return ""
    if segments[-1].lower().endswith(".git"):
        segments[-1] = segments[-1][:-4]
    return "/".join(segment for segment in segments if segment)


def _host_of(netloc: str) -> str:
    host = netloc.rsplit("@", 1)[-1]
    if host.startswith("["):  # an IPv6 literal is not a host of a hosted repository
        return ""
    host = host.rsplit(":", 1)[0].lower().rstrip(".") if ":" in host else host.lower().rstrip(".")
    return "github.com" if host == "www.github.com" else host


def parse(address: Optional[str]) -> Optional[RepoAddress]:
    """The identity of the repository an address names; None when it names no hosted repository (a local path, a
    name, an address without an owner)."""
    value = (address or "").strip()
    if not value:
        return None
    if "://" in value:
        parts = urlsplit(value)
        if parts.scheme.lower() not in SCHEMES:
            return None
        host, path = _host_of(parts.netloc), _clean_path(parts.path)
    else:
        scp = _SCP.match(value)
        if scp:
            host, path = scp.group(1).lower(), _clean_path(scp.group(2))
            host = "github.com" if host == "www.github.com" else host
        else:
            # "github.com/owner/name": an address without its scheme
            head, _, rest = value.partition("/")
            host, path = _host_of(head), _clean_path(rest)
    if not _HOST.match(host) or not path:
        return None
    segments = path.split("/")
    if len(segments) < 2:
        return None
    if host == "github.com" and len(segments) != 2:
        return None  # a page of the repository (tree/main, issues, ...), not its address
    return RepoAddress(host=host, path=path)


def key_of(address: Optional[str]) -> Optional[str]:
    parsed = parse(address)
    return parsed.key if parsed else None
