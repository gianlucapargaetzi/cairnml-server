"""Applies the identity provider's answers to CairnML users every sync interval.

Ported from the mlops server (services/user_sync.py). A user the IdP reports as gone or disabled
loses access on the next pass; a lookup that fails changes nothing. If every user comes back gone
or disabled, the directory is more likely wrong than the whole company offboarded, so nobody is
disabled in that pass.
"""

import logging
import threading

from apiserver.idp.directory import DirectoryError, DirectoryUser
from apiserver.idp.users import role_for

log = logging.getLogger("cairnml.idp.sync")


def _gone(answer) -> bool:
    return answer is None or (not isinstance(answer, DirectoryError) and not answer.enabled)


def decide(users, answers: dict, admin_role: str) -> list:
    """Return (user_id, disabled, role) for every user whose state would change.

    answers maps a user's idp_subject to a DirectoryUser, None (gone) or a DirectoryError.
    A user with no answer is treated as a failed lookup.
    """
    pairs = [(u, answers.get(u.idp_subject, DirectoryError("no answer"))) for u in users]
    mass_anomaly = len(pairs) > 1 and all(_gone(answer) for _, answer in pairs)
    actions = []
    for user, answer in pairs:
        if isinstance(answer, DirectoryError):
            continue
        if _gone(answer):
            if user.idp_disabled:
                continue
            if mass_anomaly:
                continue
            actions.append((user.id, True, role_for((), admin_role)))
        else:
            role = role_for(answer.roles, admin_role)
            if user.idp_disabled or user.role != role:
                actions.append((user.id, False, role))
    return actions


def fetch_answers(directory, users) -> dict:
    answers = {}
    for user in users:
        try:
            # The directory knows the user by its id; the stored subject is "issuer|id".
            answers[user.idp_subject] = directory.get_user(user.idp_subject.split("|", 1)[-1])
        except DirectoryError as exc:
            answers[user.idp_subject] = exc
    return answers


def audit_disabled_owner(user_id: str) -> list:
    """Record which live devices lose their owner when the sync disables a user. Returns their ids."""
    from apiserver.database.model.device import Device
    from apiserver.idp.audit import record

    device_ids = [d.id for d in Device.objects(user=user_id, revoked=False).only("id")]
    if device_ids:
        record(user_id, "owner.disabled", devices=device_ids)
        log.warning("owner %s disabled; its live devices are refused: %s", user_id, ", ".join(device_ids))
    return device_ids


def sync_once(directory, admin_role: str) -> int:
    """Run one pass against the database. Returns the number of users changed."""
    from apiserver.database.model.auth import User

    users = list(User.objects(idp_subject__ne=None))
    answers = fetch_answers(directory, users)
    actions = decide(users, answers, admin_role)
    for user_id, disabled, role in actions:
        User.objects(id=user_id).update_one(set__idp_disabled=disabled, set__role=role)
        if disabled:
            audit_disabled_owner(user_id)
    if actions:
        log.info("user sync changed %d user(s)", len(actions))
    return len(actions)


class UserSyncThread(threading.Thread):
    def __init__(self, directory, admin_role: str, interval: int) -> None:
        super().__init__(name="idp-user-sync", daemon=True)
        self._directory = directory
        self._admin_role = admin_role
        self._interval = interval
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                sync_once(self._directory, self._admin_role)
            except Exception:  # keep the loop alive; the next pass retries
                log.exception("user sync pass failed")

    def stop(self) -> None:
        self._stop.set()
