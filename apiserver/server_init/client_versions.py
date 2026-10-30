"""The versions of the SDK and the agent that call the server, read from the X-CairnML-Client header.

A client is never refused for its version. A call from a client older than the minimum in the configuration
(apiserver.client_versions) gets an X-CairnML-Upgrade response header that says what to upgrade, and the server logs
the notice once. See the server README, section "Compatibility".
"""

import re
from typing import Optional

from apiserver.config_repo import config

CLIENT_HEADERS = ("X-CairnML-Client", "X-Trains-Client")
UPGRADE_HEADER = "X-CairnML-Upgrade"
COMPATIBILITY_DOCS = "https://github.com/gianlucapargaetzi/cairnml-server/blob/main/README.md#compatibility"

log = config.logger(__file__)

# The client name in the header, its label in the notice, and the configuration key of its minimum version.
_CLIENTS = {
    "cairnml": ("SDK", "min_sdk_version"),
    "cairnml-agent": ("agent", "min_agent_version"),
    "cairnml_agent": ("agent", "min_agent_version"),
}
_VERSION_RE = re.compile(r"^\d+(\.\d+)*$")
_MAX_LOGGED = 256
_logged: set = set()


def parse_version(text: str) -> Optional[tuple]:
    """(1, 2, 3) for '1.2.3'; None for anything that is not a plain dotted number."""
    if not _VERSION_RE.fullmatch(text or ""):
        return None
    return tuple(int(part) for part in text.split("."))


def parse_header(value: str) -> list:
    """(name, version) for each entry of the header, e.g. 'cairnml-0.0.2, cairnml_agent-0.0.1'."""
    entries = []
    for item in (value or "").split(","):
        name, sep, version = item.strip().rpartition("-")
        if sep and name:
            entries.append((name, version))
    return entries


def _minimum(key: str) -> str:
    return config.get(f"apiserver.client_versions.{key}", "") or ""


def outdated(header_value: str) -> list:
    """(name, version, minimum) for each client in the header that is older than its minimum."""
    found = []
    for name, version in parse_header(header_value):
        if name not in _CLIENTS:
            continue
        minimum = _minimum(_CLIENTS[name][1])
        have, need = parse_version(version), parse_version(minimum)
        if have and need and have < need:
            found.append((name, version, minimum))
    return found


def upgrade_notice(headers) -> Optional[str]:
    """The text of the X-CairnML-Upgrade header for these request headers; None when every client is current."""
    value = next((headers[h] for h in CLIENT_HEADERS if h in headers), None)
    old = outdated(value)
    if not old:
        return None
    parts = [
        f"the {_CLIENTS[name][0]} ({name}) {version} is older than this server's minimum {minimum}: upgrade {name}"
        for name, version, minimum in old
    ]
    return "; ".join(parts) + f". See {COMPATIBILITY_DOCS}"


def note(headers) -> Optional[str]:
    """The notice for these request headers, logged once per distinct notice."""
    notice = upgrade_notice(headers)
    if notice and notice not in _logged:
        if len(_logged) >= _MAX_LOGGED:
            _logged.clear()
        _logged.add(notice)
        log.warning(notice)
    return notice
