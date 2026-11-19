"""
Access guards for tasks and models: who may read, change or delete an entity, which entities a search
may return, and the rule for moving an entity to another project. The services call these; no service
module defines a guard of its own.
"""

from typing import Callable, Optional, Sequence

from mongoengine import Q

from apiserver.apierrors import errors
from apiserver.bll.project.access import ProjectAccessBLL
from apiserver.bll.task.utils import get_many_tasks_for_writing
from apiserver.database.model.model import Model
from apiserver.database.model.task.task import Task
from apiserver.service_repo.auth import Identity

# The fields an access check reads; the same for tasks and models
ACCESS_FIELDS = ("id", "company", "project", "user")


def _task_not_found(identity: Identity, task: Task) -> Exception:
    return errors.bad_request.InvalidTaskId(id=task.id, company=identity.company)


def _model_not_found(identity: Identity, model: Model) -> Exception:
    return errors.bad_request.InvalidModelId(company=identity.company, id=model.id)


def visible_entities_query(identity: Identity) -> Optional[Q]:
    """
    Restricts a task or model search to the projects the caller can see. Entities without a project keep
    the company-level visibility they had before. None means no restriction (admin)
    """
    visible = ProjectAccessBLL.visible_project_ids(identity)
    if visible is None:
        return None
    return Q(project__in=list(visible)) | Q(project=None) | Q(project="")


def scope_entities_query(query: Q, identity: Identity) -> Q:
    scope = visible_entities_query(identity)
    return query if scope is None else query & scope


def assert_entity_access(
    identity: Identity,
    entity,
    capability: str,
    not_found: Callable[[Identity, object], Exception],
) -> None:
    """
    Check `capability` on the project of the entity, with the entity owner deciding own-object rights.
    An entity without a project has no project to check against and keeps its company-level behaviour.
    A caller who cannot see the project gets the same error as for a missing entity (`not_found`), so a
    hidden entity does not reveal its project
    """
    if not entity.project:
        return
    if ProjectAccessBLL.get_role(identity, entity.project) is None:
        raise not_found(identity, entity)
    ProjectAccessBLL.assert_can(identity, entity.project, capability, object_owner=entity.user)


def assert_entities_access(
    entity_cls,
    company_id: str,
    identity: Identity,
    ids: Sequence[str],
    capability: str,
    not_found: Callable[[Identity, object], Exception],
) -> None:
    """
    Check `capability` on every existing entity of `ids` before any of them is changed, so that a batch
    is all-or-nothing. Missing ids are not an error here: the endpoint reports them as it did before
    """
    entities = entity_cls.get_many(
        company=company_id,
        query=Q(id__in=list(ids)),
        override_projection=ACCESS_FIELDS,
        allow_public=True,
        return_dicts=False,
    )
    for entity in entities:
        assert_entity_access(identity, entity, capability, not_found)


def assert_task_access(identity: Identity, task: Task, capability: str) -> None:
    assert_entity_access(identity, task, capability, _task_not_found)


def assert_tasks_access(company_id: str, identity: Identity, ids: Sequence[str], capability: str) -> None:
    assert_entities_access(Task, company_id, identity, ids, capability, _task_not_found)


def assert_model_access(identity: Identity, model: Model, capability: str) -> None:
    assert_entity_access(identity, model, capability, _model_not_found)


def assert_models_access(company_id: str, identity: Identity, ids: Sequence[str], capability: str) -> None:
    assert_entities_access(Model, company_id, identity, ids, capability, _model_not_found)


def assert_writable_tasks(company_id: str, identity: Identity, ids: Sequence[str], only=("id",)) -> Sequence[Task]:
    tasks = get_many_tasks_for_writing(
        company_id=company_id,
        identity=identity,
        query=Q(id__in=ids),
        only=only,
    )
    missing_ids = set(ids) - {t.id for t in tasks}
    if missing_ids:
        raise errors.bad_request.InvalidTaskId(ids=list(missing_ids))

    return tasks


def assert_destination_project(identity: Identity, entity, fields: dict) -> None:
    """
    A task or model moved to another project by update or edit needs admin on that project, the same rule
    the `move` endpoint applies. Otherwise a collaborator could move it through the generic edit path that
    `move` gates behind admin. Clearing the project needs admin on the current one: a projectless entity is
    open to the whole company, so clearing is a move out of the project
    """
    if "project" not in fields:
        return
    new_project = fields["project"]
    if new_project == entity.project:
        return
    if new_project:
        ProjectAccessBLL.assert_can(identity, new_project, "admin")
    elif entity.project:
        ProjectAccessBLL.assert_can(identity, entity.project, "admin")
