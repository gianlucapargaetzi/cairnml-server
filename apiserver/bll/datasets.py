"""The datasets of the environment: who may see, change, allocate and delete them, and which credential reads each one.

Visibility is one of ``user`` (the owner, and admins), ``projects`` (the owner, admins and members of the projects the
dataset is allocated to) and ``Public`` (every user of the company). A dataset without a working credential is kept, and
its state says so. See docs/superpowers/specs/2026-11-05-datasets-access-design.md.

The functions here that take plain dicts do no database work, so they can be tested without one.
"""

NO_ACCESS = "no_access"
OK = "ok"


def visible_to(dataset: dict, user: str, user_projects: set, is_admin: bool) -> bool:
    """True when the user may see the dataset under its visibility."""
    if is_admin or dataset.get("owner") == user:
        return True
    visibility = dataset.get("visibility")
    if visibility == "Public":
        return True
    if visibility == "projects":
        return bool(set(dataset.get("projects") or []) & set(user_projects))
    return False


def credential_state(dataset: dict, credential_ids: set) -> tuple:
    """(state, message) for a dataset given the credentials the viewer can still use. A public dataset (no credential)
    is always ok. A dataset whose credential is gone has no access, and the message is the banner text."""
    credential = dataset.get("credential")
    if not credential or credential in credential_ids:
        return OK, ""
    return NO_ACCESS, f"no access to dataset {dataset.get('name')}"


def can_manage(dataset: dict, user: str, is_admin: bool) -> bool:
    """Only the owner, and admins, may change, allocate or delete a dataset."""
    return is_admin or dataset.get("owner") == user
