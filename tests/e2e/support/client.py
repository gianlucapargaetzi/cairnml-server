"""A person's machine as the CairnML SDK enrols it: cairnml init, cairnml whoami and cairnml logout."""

import re
import subprocess
import sys
from pathlib import Path

from . import cli, settings

# The SDK and the agent name their configuration files in their own code (the SDK's default_config_file, the agent's).
# They expand ~ when they are imported, so each is asked in a process whose HOME is the home under test.
SDK_CONFIG_QUERY = "from cairnml.cli.enrolment import default_config_file; print(default_config_file())"
AGENT_CONFIG_QUERY = "from cairnml_agent.commands.config import default_config_file; print(default_config_file())"


def key_path(home: Path) -> Path:
    return home / ".cairnml" / "device.key"


def agent_key_path(home: Path) -> Path:
    """The agent's own device key (cairnml-agent init), apart from the SDK's."""
    return home / ".cairnml" / "agent" / "device.key"


def _named_by_sdk(home: Path, query: str) -> Path:
    proc = subprocess.run(
        [sys.executable, "-c", query], env=cli.environment(home), capture_output=True, text=True, timeout=120, check=True
    )
    return Path(proc.stdout.strip())


def config_file(home: Path) -> Path:
    """The configuration file cairnml init writes in this home."""
    return _named_by_sdk(home, SDK_CONFIG_QUERY)


def agent_config_file(home: Path) -> Path:
    """The configuration file cairnml-agent init writes in this home."""
    return _named_by_sdk(home, AGENT_CONFIG_QUERY)


def device_id_of(home: Path) -> str:
    """The device id this home is enrolled as, read from its configuration file."""
    return _device_id_in(config_file(home))


def agent_device_id_of(home: Path) -> str:
    """The device id the agent in this home is enrolled as, read from the agent's configuration file."""
    return _device_id_in(agent_config_file(home))


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
