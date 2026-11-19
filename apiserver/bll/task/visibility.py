"""
Listing rules for tasks: which tasks a search returns by default. Who may see a task is decided by the access
guards in bll/project/entity_access.py; this module only decides the system tag `hidden`.
"""

from mongoengine import Q

from apiserver.database.model import EntityVisibility


def hidden_query(data: dict) -> Q:
    """
    1. Add only non-hidden tasks search condition (unless specifically specified differently)
    """
    if data.get("search_hidden") or data.get("id"):
        return Q()

    return Q(system_tags__ne=EntityVisibility.hidden.value)
