"""The stages of cloning a task, as functions that can be tested alone. TaskBLL.clone_task runs them in order."""
from typing import Optional

from apiserver.database.model.task.task import Task
from apiserver.utilities.dicts import nested_set


def merged_hyperparams(
    task: Task, hyperparams: Optional[dict], hyperparams_overrides: Optional[dict]
) -> Optional[dict]:
    """The hyperparams of the clone: the given ones as they are, else the source's with the overrides set per
    section and key, else None (the clone then keeps the source's)."""
    if hyperparams:
        return hyperparams
    if not hyperparams_overrides:
        return None

    updated_hyperparams = {
        sec: {k: value for k, value in sec_data.items()}
        for sec, sec_data in (task.hyperparams or {}).items()
    }
    for section, section_data in hyperparams_overrides.items():
        for key, value in section_data.items():
            nested_set(updated_hyperparams, (section, key), value)
    return updated_hyperparams


def merged_configuration(
    task: Task, configuration: Optional[dict], configuration_overrides: Optional[dict]
) -> Optional[dict]:
    """The configuration of the clone: the given one as it is, else the source's with the overrides set per key,
    else None (the clone then keeps the source's)."""
    if configuration:
        return configuration
    if not configuration_overrides:
        return None

    updated_configuration = {k: value for k, value in (task.configuration or {}).items()}
    for key, value in configuration_overrides.items():
        updated_configuration[key] = value
    return updated_configuration
