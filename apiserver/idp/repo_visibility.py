"""The check made when a job on a Git host other than GitHub is submitted. A private repository needs Git
credentials stored for its host (or path prefix); without them the job is refused at once, naming the host.
A public repository is accepted, and so is one whose host cannot be asked: the agent then fails on its clone.

The host is asked anonymously with the smart HTTP discovery request that every Git host serves. A host that
answers with the upload-pack advertisement allows anonymous reads; one that refuses (401, 403, 404, or a sign-in
page) does not."""

import logging
from typing import Optional
from urllib.parse import urlsplit

import requests

from apiserver.apierrors import errors
from apiserver.idp import git_credentials as store

log = logging.getLogger("cairnml.idp.repo_visibility")
PROBE_TIMEOUT_SEC = 10
ADVERTISEMENT = "application/x-git-upload-pack-advertisement"


def _probe_url(repository: str) -> Optional[tuple]:
    """(host, URL of the discovery request) for an HTTP(S) repository URL, or None for anything else."""
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
    return host.lower(), f"{parts.scheme}://{host}/{path}/info/refs?service=git-upload-pack"


def is_private(repository: str) -> Optional[bool]:
    """True when the host refuses an anonymous read, False when it allows one, None when it cannot be asked."""
    probe = _probe_url(repository)
    if probe is None:
        return None
    _host, url = probe
    try:
        response = requests.get(url, timeout=PROBE_TIMEOUT_SEC)
    except requests.RequestException:
        log.info("could not ask %s whether a repository is public", _host)
        return None
    if response.status_code >= 500:
        return None
    public = response.status_code == 200 and response.headers.get("Content-Type", "").startswith(ADVERTISEMENT)
    return not public


def check_readable(company: str, repository: str) -> None:
    """Raise a validation error when a private repository on this host has no stored credentials."""
    if is_private(repository) is not True:
        return
    if store.find_credential(company, repository) is not None:
        return
    host, _path = store.host_and_path(repository)
    raise errors.bad_request.ValidationError(
        f"the repository is private and no Git credentials are stored for {host}: an admin adds them with the "
        "git_credentials.set call (the web app's Settings page), or the repository must be made public")
