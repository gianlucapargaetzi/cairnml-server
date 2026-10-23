"""Secrets are read from files in one read-only mount, never from configuration files."""

import os

DEFAULT_SECRETS_DIR = "/run/secrets-dir"


class SecretMissing(Exception):
    pass


def secrets_dir() -> str:
    return os.environ.get("CAIRNML_SECRETS_DIR", DEFAULT_SECRETS_DIR)


def read_secret(name: str) -> str:
    """Return the secret stored in <secrets dir>/<name>, without one trailing newline."""
    path = os.path.join(secrets_dir(), name)
    try:
        with open(path, encoding="utf-8") as handle:
            value = handle.read()
    except FileNotFoundError:
        raise SecretMissing(f"secret file {name!r} is missing from {secrets_dir()}") from None
    return value[:-1] if value.endswith("\n") else value


def refuse_default_secret(value: str, default: str) -> None:
    """Refuse to run with the public default that ships in the repository."""
    if value == default:
        raise ValueError("the default secret from the repository must not be used; set a real one")


# The public default that ships in config/default/secure.conf. A server must not run with it.
DEFAULT_SESSION_SECRET = "V8gcW3EneNDcNfO7G_TSUsWe7uLozyacc9_I33o7bxUo8rCN31VLRg"


def session_secret() -> str:
    """The web session key: read from the secrets folder. The configuration value is used only when
    CAIRNML_ALLOW_CONFIG_SECRET=1 (tests), and the repository default is refused otherwise."""
    if os.environ.get("CAIRNML_ALLOW_CONFIG_SECRET") == "1":
        from apiserver.config_repo import config

        return config.get("secure.http.session_secret.apiserver")
    value = read_secret("session_secret")
    refuse_default_secret(value, DEFAULT_SESSION_SECRET)
    return value
