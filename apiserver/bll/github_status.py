"""The GitHub App check: whether the App ID and its key work, and what the key reaches.

It runs in four steps, and a failed step stops the rest:

1. configured: GITHUB_APP_ID is set and the key file is readable.
2. app: GitHub accepts the App ID and key (GET /app with a token made from the key).
3. repositories: the repositories the App is installed on, and their count.
4. registries: every dataset registry, and the datasets each one lists.

The result is kept in the database, so every API worker shows the same status. It runs once when the server starts and
again on demand (github.check). The admin sees it as a banner after sign-in.
"""

import logging
import threading
from datetime import datetime

from apiserver.database.model.settings import Settings
from apiserver.idp.github import GitHubError, GitHubRefused
from apiserver.idp.github_check import build_from_config
from apiserver.idp.secrets import SecretMissing

STATUS_SETTING = "github.status"
log = logging.getLogger(__name__)


def _plural(count: int, word: str, plural: str = None) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {plural or word + 's'}"


def _result(state: str, steps: list, message: str, **extra) -> dict:
    return {
        "state": state,
        "message": message,
        "steps": steps,
        "checked_at": datetime.utcnow().isoformat(),
        **extra,
    }


def run_check() -> dict:
    """Runs the four steps against GitHub now. Does not save the result."""
    from apiserver.bll import dataset_registry as registry_bll
    from apiserver.config_repo import config

    steps = []

    def step(name: str, ok: bool, detail: str) -> None:
        steps.append({"name": name, "ok": ok, "detail": detail})

    if not config.get("apiserver.idp.github_app_id", ""):
        step("configured", False, "GITHUB_APP_ID is not set in deploy/.env")
        return _result("not_configured", steps, "GitHub App is not configured: GITHUB_APP_ID is not set")

    try:
        app = build_from_config()
    except SecretMissing as exc:
        step("configured", False, str(exc))
        return _result("refused", steps, f"GitHub App Access Refused: {exc}")
    step("configured", True, "the App ID is set and the key is readable")

    try:
        info = app.app_info()
    except GitHubRefused as exc:
        step("app", False, str(exc))
        return _result("refused", steps, f"GitHub App Access Refused: {exc}")
    except GitHubError as exc:
        step("app", False, str(exc))
        return _result("unreachable", steps, f"GitHub could not be reached: {exc}")
    step("app", True, f"GitHub accepts the App {info.get('slug') or info.get('name') or info.get('id')}")

    try:
        repositories = app.installed_repositories()
    except GitHubError as exc:
        step("repositories", False, str(exc))
        return _result("unreachable", steps, f"GitHub could not be reached: {exc}", app=info)
    step("repositories", True, f"the App is installed on {_plural(len(repositories), 'repository')}")

    registries, datasets_total = [], 0
    for name in registry_bll.configured_registries():
        try:
            entries = registry_bll.list_entries(app=app, registry=name)
        except GitHubError as exc:
            registries.append({"repository": name, "ok": False, "datasets": 0, "detail": str(exc)})
            step(f"registry {name}", False, str(exc))
            continue
        registries.append({"repository": name, "ok": True, "datasets": len(entries), "detail": ""})
        datasets_total += len(entries)
        step(f"registry {name}", True, f"lists {_plural(len(entries), 'dataset')}")

    readable = sum(1 for registry in registries if registry["ok"])
    message = (f"GitHub App works. Registered {_plural(len(registries), 'dataset registry', 'dataset registries')} "
               f"with {_plural(datasets_total, 'dataset')}")
    if registries and readable < len(registries):
        message += f" ({len(registries) - readable} could not be read)"
    return _result("ok", steps, message + ".", app=info, repositories=repositories,
                   registries=registries, datasets=datasets_total)


_CONTACT_ADMIN = "If something does not work, contact your admin."
_USER_MESSAGES = {
    "refused": f"GitHub App Access Refused. {_CONTACT_ADMIN}",
    "unreachable": f"GitHub could not be reached. {_CONTACT_ADMIN}",
    "not_configured": f"The GitHub App is not set up on this server. {_CONTACT_ADMIN}",
    "unknown": "The GitHub App has not been checked yet.",
}


def user_view(result: dict) -> dict:
    """What a user who is not an admin may see: the state and one line. The reasons stay with the admins. A working
    App keeps its line, which only has the counts in it."""
    state = result.get("state", "unknown")
    message = result.get("message", "") if state == "ok" else _USER_MESSAGES.get(state, _USER_MESSAGES["unknown"])
    return {"state": state, "message": message, "steps": [], "checked_at": result.get("checked_at")}


def save_check() -> dict:
    result = run_check()
    Settings.set_or_add_value(STATUS_SETTING, result)
    if result["state"] != "ok":
        log.warning("GitHub App check: %s", result["message"])
    return result


def get_status():
    """The last saved check, or None when none has run yet."""
    return Settings.get_by_key(STATUS_SETTING, default=None)


def start_check_in_background() -> None:
    """Runs the check at server start without holding up the start. A failure is logged and kept as the status."""

    def worker():
        try:
            save_check()
        except Exception:
            log.exception("GitHub App check failed unexpectedly")

    threading.Thread(target=worker, name="github-app-check", daemon=True).start()
