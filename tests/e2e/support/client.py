"""A person's machine as the CairnML SDK enrols it: cairnml init, cairnml whoami and cairnml logout."""

import re
from pathlib import Path

from . import cli, settings


def key_path(home: Path) -> Path:
    return home / ".cairnml" / "device.key"


def agent_key_path(home: Path) -> Path:
    """The agent's own device key (cairnml-agent init), apart from the SDK's."""
    return home / ".cairnml" / "agent" / "device.key"


def device_id_of(home: Path) -> str:
    """The device id this home is enrolled as, read from its configuration file."""
    return _device_id_in(home / "cairnml.conf")


def agent_device_id_of(home: Path) -> str:
    """The device id the agent in this home is enrolled as, read from the agent's configuration file."""
    return _device_id_in(home / "cairnml-agent.conf")


def _device_id_in(conf: Path) -> str:
    if not conf.is_file():
        return ""
    match = re.search(r'device_id\s*[:=]\s*"?(dev_[0-9a-f]{32})', conf.read_text())
    return match.group(1) if match else ""


def init(home: Path, account, *, server: str = None, name: str = "e2e-machine", force: bool = False):
    """cairnml init <server>, signed in as account (user, password) through the sign-in page. The server is the
    stack's address; the API is under /api of it."""
    user, password = account
    args = ["init", server or settings.WEB_URL, "--name", name]
    if force:
        args.append("--force")
    return cli.run("cairnml", home, login_user=user, login_password=password, args=args)


def whoami(home: Path):
    return cli.run("cairnml", home, args=["whoami"])


def logout(home: Path):
    return cli.run("cairnml", home, args=["logout"])
