from datetime import datetime, timedelta
from itertools import chain
from operator import itemgetter
from typing import (
    Sequence,
    Optional,
    Type,
    Tuple,
    Dict,
    Set,
    Mapping,
    Any,
    Union,
)

from mongoengine import Q, Document

from apiserver import database
from apiserver.apierrors import errors
from apiserver.apimodels.projects import ProjectChildrenType
from apiserver.bll.project.access import ProjectAccessBLL
from apiserver.config_repo import config
from apiserver.database.model import EntityVisibility, UserProfile
from apiserver.database.model.base import GetMixin
from apiserver.database.model.model import Model
from apiserver.database.model.project import Project
from apiserver.database.model.task.task import Task, external_task_types
from apiserver.database.utils import get_company_or_none_constraint
from apiserver.service_repo.auth import Identity
from .sub_projects import (
    _reposition_project_with_children,
    _ensure_project,
    _validate_project_name,
    _update_subproject_names,
    _save_under_parent,
    _get_sub_projects,
    _ids_with_children,
    _ids_with_parents,
    _get_project_depth,
    ProjectsChildren,
    _get_writable_project_from_name,
    _get_basename_from_name,
)
from .stats import (
    count_by_field_pipeline,
    contributions_pipeline,
    contributions_period_start,
    contributions_response,
    contributor_ids,
    counts_by_project,
    dataset_pipeline,
    dataset_runtime_by_project,
    dataset_runtime_pipeline,
    dataset_stats_by_project,
    active_stats_by_project,
    children_info,
    match_conditions,
    own_contents_by_project,
    own_count_by_project,
    project_runtime_pipeline,
    runtime_by_project,
    selected_children_queries,
    selected_ids_within,
    with_non_archived,
    with_system_tag,
)

# The tags and the statistics project names are defined with the statistics; these re-exports keep the names
# importable from this module, where the services and the project cleanup import them from.
from .stats import dataset_tag as dataset_tag  # noqa: E402
from .stats import datasets_project_name as datasets_project_name  # noqa: E402
from .stats import pipeline_tag as pipeline_tag  # noqa: E402
from .stats import pipelines_project_name as pipelines_project_name  # noqa: E402
from .stats import reports_tag as reports_tag  # noqa: E402

log = config.logger(__file__)
max_depth = config.get("services.projects.sub_projects.max_depth", 10)
reports_project_name = ".reports"
class ProjectBLL:
    child_classes = (Task, Model)

    @classmethod
    def merge_project(cls, company: str, source_id: str, destination_id: str) -> Tuple[int, int, Set[str]]:
        """
        Move all the tasks and sub projects from the source project to the destination
        Remove the source project
        Return the amounts of moved entities and subprojects + set of all the affected project ids
        """
        if source_id == destination_id:
            raise errors.bad_request.ProjectSourceAndDestinationAreTheSame(source=source_id)
        source = Project.get(company, source_id)
        if destination_id:
            destination = Project.get(company, destination_id)
            if source_id in destination.path:
                raise errors.bad_request.ProjectCannotBeMergedIntoItsChild(source=source_id, destination=destination_id)
        else:
            destination = None

        children = _get_sub_projects([source.id], _only=("id", "name", "parent", "path"))[source.id]
        if destination:
            cls.validate_projects_depth(
                projects=children,
                old_parent_depth=len(source.path) + 1,
                new_parent_depth=len(destination.path) + 1,
            )

        moved_entities = 0
        for entity_type in cls.child_classes:
            moved_entities += entity_type.objects(
                company=company,
                project=source_id,
                system_tags__nin=[EntityVisibility.archived.value],
            ).update(upsert=False, project=destination_id)

        moved_sub_projects = 0
        for child in Project.objects(company=company, parent=source_id):
            _reposition_project_with_children(
                project=child,
                children=[c for c in children if c.parent == child.id],
                parent=destination,
            )
            moved_sub_projects += 1

        affected = {source.id, *(source.path or [])}
        source.delete()

        if destination:
            destination.update(last_update=datetime.utcnow())
            affected.update({destination.id, *(destination.path or [])})

        return moved_entities, moved_sub_projects, affected

    @staticmethod
    def validate_projects_depth(projects: Sequence[Project], old_parent_depth: int, new_parent_depth: int):
        for current in projects:
            current_depth = len(current.path) + 1
            if current_depth - old_parent_depth + new_parent_depth > max_depth:
                raise errors.bad_request.ProjectPathExceedsMax(max_depth=max_depth)

    @classmethod
    def move_project(
        cls,
        company: str,
        user: str,
        identity: Identity,
        project_id: str,
        new_location: str,
    ) -> Tuple[int, Set[str]]:
        """
        Move project with its sub projects from its current location to the target one.
        If the target location does not exist then it will be created. If it exists then
        it should be writable. The source location should be writable too.
        Return the number of moved projects + set of all the affected project ids
        """
        project = Project.get(company, project_id)
        old_parent_id = project.parent
        old_parent = Project.get_for_writing(company=project.company, id=old_parent_id) if old_parent_id else None

        children = _get_sub_projects([project.id], _only=("id", "name", "path"))[project.id]
        cls.validate_projects_depth(
            projects=[project, *children],
            old_parent_depth=len(project.path),
            new_parent_depth=_get_project_depth(new_location),
        )

        new_parent = _ensure_project(company=company, user=user, name=new_location, identity=identity)
        new_parent_id = new_parent.id if new_parent else None
        if old_parent_id == new_parent_id:
            raise errors.bad_request.ProjectSourceAndDestinationAreTheSame(
                location=new_parent.name if new_parent else ""
            )
        if new_parent and (project_id == new_parent.id or project_id in new_parent.path):
            raise errors.bad_request.ProjectCannotBeMovedUnderItself(project=project_id, parent=new_parent.id)
        moved = _reposition_project_with_children(project, children=children, parent=new_parent)

        now = datetime.utcnow()
        affected = set()
        p: Project
        for p in filter(None, (old_parent, new_parent)):
            p.update(last_update=now)
            affected.update({p.id, *(p.path or [])})

        return moved, affected

    @classmethod
    def update(cls, company: str, project_id: str, **fields):
        project = Project.get_for_writing(company=company, id=project_id)
        if not project:
            raise errors.bad_request.InvalidProjectId(id=project_id)

        new_name = fields.pop("name", None)
        if new_name:
            # noinspection PyTypeChecker
            new_name, new_location = _validate_project_name(new_name)
            old_name, old_location = _validate_project_name(project.name)
            if new_location != old_location:
                raise errors.bad_request.CannotUpdateProjectLocation(name=new_name)
            fields["name"] = new_name
            fields["basename"] = _get_basename_from_name(new_name)

        fields["last_update"] = datetime.utcnow()
        updated = project.update(upsert=False, **fields)

        if new_name:
            old_name = project.name
            project.name = new_name
            children = _get_sub_projects([project.id], _only=("id", "name", "path"))[project.id]
            _update_subproject_names(project=project, children=children, old_name=old_name)

        return updated

    @classmethod
    def create(
        cls,
        user: str,
        company: str,
        name: str,
        identity: Identity,
        description: str = "",
        tags: Sequence[str] = None,
        system_tags: Sequence[str] = None,
        default_output_destination: str = None,
        parent_creation_params: dict = None,
    ) -> str:
        """
        Create a new project.
        Returns project ID
        """
        if _get_project_depth(name) > max_depth:
            raise errors.bad_request.ProjectPathExceedsMax(max_depth=max_depth)

        name, location = _validate_project_name(name)

        existing = _get_writable_project_from_name(
            company=company,
            name=name,
            identity=identity,
        )
        if existing:
            raise errors.bad_request.ExpectedUniqueData(
                replacement_msg="Project with the same name already exists",
                name=name,
                company=company,
            )

        now = datetime.utcnow()
        project = Project(
            id=database.utils.id(),
            user=user,
            company=company,
            name=name,
            basename=_get_basename_from_name(name),
            description=description,
            tags=tags,
            system_tags=system_tags,
            default_output_destination=default_output_destination,
            created=now,
            last_update=now,
        )
        parent = _ensure_project(
            company=company,
            user=user,
            name=location,
            identity=identity,
            creation_params=parent_creation_params,
        )
        _save_under_parent(project=project, parent=parent)
        if parent:
            parent.update(last_update=now)

        return project.id

    @classmethod
    def find_or_create(
        cls,
        user: str,
        company: str,
        identity: Identity,
        project_name: str,
        description: str,
        project_id: str = None,
        tags: Sequence[str] = None,
        system_tags: Sequence[str] = None,
        default_output_destination: str = None,
        parent_creation_params: dict = None,
    ) -> str:
        """
        Find a project named `project_name` or create a new one.
        Returns project ID.
        The caller must have write access to the project that is returned or to the project the new
        one is created in, so that no content is placed in a project it cannot write to
        """
        if not project_id and not project_name:
            raise errors.bad_request.ValidationError("project id or name required")

        if project_id:
            project = Project.objects(company=company, id=project_id).only("id").first()
            if not project:
                raise errors.bad_request.InvalidProjectId(id=project_id)
            ProjectAccessBLL.assert_can(identity, project_id, "write", object_owner=identity.user)
            return project_id

        project_name, _ = _validate_project_name(project_name)
        # Checks the project found by this name, or the deepest visible parent a new one is created in
        ProjectAccessBLL.assert_can_write_destination(identity, company, project_name)
        project = _get_writable_project_from_name(company=company, name=project_name, identity=identity)
        if project:
            return project.id

        return cls.create(
            user=user,
            company=company,
            identity=identity,
            name=project_name,
            description=description,
            tags=tags,
            system_tags=system_tags,
            default_output_destination=default_output_destination,
            parent_creation_params=parent_creation_params,
        )

    @classmethod
    def move_under_project(
        cls,
        entity_cls: Type[Document],
        user: str,
        company: str,
        identity: Identity,
        ids: Sequence[str],
        project: str = None,
        project_name: str = None,
    ):
        """
        Move a batch of entities to `project` or a project named `project_name` (create if does not exist)
        """
        if project_name or project:
            project = cls.find_or_create(
                user=user,
                company=company,
                identity=identity,
                project_id=project,
                project_name=project_name,
                description="",
            )

        extra = {}
        if hasattr(entity_cls, "last_change"):
            extra["set__last_change"] = datetime.utcnow()
        if hasattr(entity_cls, "last_changed_by"):
            extra["set__last_changed_by"] = user

        entity_cls.objects(company=company, id__in=ids).update(set__project=project, **extra)

        return project

    @staticmethod
    def _get_projects_children(
        project_ids: Sequence[str],
        search_hidden: bool,
        allowed_ids: Sequence[str],
    ) -> Tuple[ProjectsChildren, Set[str]]:
        child_projects = _get_sub_projects(
            project_ids,
            _only=("id", "name"),
            search_hidden=search_hidden,
            allowed_ids=allowed_ids,
        )
        return (
            child_projects,
            {c.id for c in chain.from_iterable(child_projects.values())},
        )

    @classmethod
    def _with_children(
        cls,
        project_ids: Sequence[str],
        include_children: bool,
        search_hidden: bool,
        allowed_ids: Optional[Sequence[str]],
    ) -> Tuple[ProjectsChildren, Set[str]]:
        """
        The subprojects of the projects (when included) and the ids of the projects with their subprojects
        """
        child_projects = {}
        ids_with_children = set(project_ids)
        if include_children:
            child_projects, children_ids = cls._get_projects_children(
                project_ids,
                search_hidden=search_hidden,
                allowed_ids=allowed_ids,
            )
            ids_with_children |= children_ids
        return child_projects, ids_with_children

    @classmethod
    def get_dataset_stats(
        cls,
        company: str,
        project_ids: Sequence[str],
        users: Sequence[str] = None,
    ) -> Dict[str, dict]:
        if not project_ids:
            return {}

        rows = Task.aggregate(dataset_runtime_pipeline(company, project_ids, users))
        return dataset_runtime_by_project(rows)

    @classmethod
    def get_project_dataset_stats(
        cls,
        company: str,
        project_ids: Sequence[str],
        include_children: bool = True,
        filter_: Mapping[str, Any] = None,
        users: Sequence[str] = None,
        selected_project_ids: Sequence[str] = None,
    ) -> Tuple[Dict[str, dict], Dict[str, dict]]:
        if not project_ids:
            return {}, {}

        child_projects, ids_with_children = cls._with_children(
            project_ids,
            include_children=include_children,
            search_hidden=True,
            allowed_ids=selected_project_ids,
        )
        match = match_conditions(
            company=company,
            project_ids=list(ids_with_children),
            filter_=with_system_tag(filter_, dataset_tag),
            users=users,
            project_field="parent",
        )
        rows = Project.aggregate(dataset_pipeline(match, project_field="parent"))
        return (
            dataset_stats_by_project(rows, project_ids, child_projects),
            children_info(project_ids, child_projects),
        )

    @classmethod
    def get_project_stats(
        cls,
        company: str,
        project_ids: Sequence[str],
        include_children: bool = True,
        search_hidden: bool = False,
        filter_: Mapping[str, Any] = None,
        users: Sequence[str] = None,
        selected_project_ids: Sequence[str] = None,
    ) -> Tuple[Dict[str, dict], Dict[str, dict]]:
        if not project_ids:
            return {}, {}

        child_projects, ids_with_children = cls._with_children(
            project_ids,
            include_children=include_children,
            search_hidden=search_hidden,
            allowed_ids=selected_project_ids,
        )
        time_thresh = datetime.utcnow() - timedelta(hours=24)
        rows = Task.aggregate(
            project_runtime_pipeline(company, list(ids_with_children), filter_, users, time_thresh=time_thresh)
        )
        return (
            active_stats_by_project(project_ids, runtime_by_project(rows), child_projects),
            children_info(project_ids, child_projects),
        )

    @classmethod
    def get_active_users(
        cls,
        company,
        project_ids: Sequence[str],
        user_ids: Optional[Sequence[str]] = None,
    ) -> Set[Union[str, type(None)]]:
        """
        Get the set of user ids that created tasks/models in the given projects
        If project_ids is empty then all projects are examined
        If user_ids are passed then only subset of these users is returned
        """
        query = Q(company=company)
        if user_ids:
            query &= Q(user__in=user_ids)

        projects_query = query
        if project_ids:
            project_ids = _ids_with_children(project_ids)
            query &= Q(project__in=project_ids)
            projects_query &= Q(id__in=project_ids)

        res = set(Project.objects(projects_query).distinct(field="user"))
        for cls_ in cls.child_classes:
            res |= set(cls_.objects(query).distinct(field="user"))

        return res

    @classmethod
    def get_project_tags(
        cls,
        company_id: str,
        include_system: bool,
        projects: Sequence[str] = None,
        filter_: Dict[str, Sequence[str]] = None,
    ) -> Tuple[Sequence[str], Sequence[str]]:
        query = Q(company=company_id)
        if filter_:
            for name, vals in filter_.items():
                if vals:
                    query &= GetMixin.get_list_field_query(name, vals)

        if projects:
            query &= Q(id__in=_ids_with_children(projects))

        tags = Project.objects(query).distinct("tags")
        system_tags = Project.objects(query).distinct("system_tags") if include_system else []
        return tags, system_tags

    @classmethod
    def get_projects_with_selected_children(
        cls,
        company: str,
        users: Sequence[str] = None,
        project_ids: Optional[Sequence[str]] = None,
        allow_public: bool = True,
        children_type: ProjectChildrenType = None,
        children_tags: Sequence[str] = None,
        children_tags_filter: dict = None,
    ) -> Tuple[Sequence[str], Sequence[str]]:
        """
        Get the projects ids matching children_condition (if passed) or where the passed user created any tasks
        including all the parents of these projects
        If project ids are specified then filter the results by these project ids
        """
        project_query, child_queries = selected_children_queries(
            company=company,
            users=users,
            allow_public=allow_public,
            children_type=children_type,
            children_tags=children_tags,
            children_tags_filter=children_tags_filter,
            project_ids=project_ids,
            with_children=_ids_with_children,
        )

        res = set(Project.objects(project_query).scalar("id")) if project_query else set()
        for cls_, query_ in child_queries.items():
            res |= set(cls_.objects(query_).distinct(field="id" if cls_ is Project else "project"))

        res = list(res)
        if not res:
            return res, res

        selected_project_ids = _ids_with_parents(res)
        return selected_ids_within(selected_project_ids, project_ids), selected_project_ids

    @staticmethod
    def _get_project_query(
        company: str,
        projects: Sequence,
        include_subprojects: bool = True,
        state: Optional[EntityVisibility] = None,
    ) -> Q:
        query = get_company_or_none_constraint(company)
        if projects:
            if include_subprojects:
                projects = _ids_with_children(projects)
            query &= Q(project__in=projects)
        # else:
        #     query &= Q(system_tags__nin=[EntityVisibility.hidden.value])

        if state == EntityVisibility.archived:
            query &= Q(system_tags__in=[EntityVisibility.archived.value])
        elif state == EntityVisibility.active:
            query &= Q(system_tags__nin=[EntityVisibility.archived.value])

        return query

    @classmethod
    def get_task_parents(
        cls,
        company_id: str,
        projects: Sequence[str],
        include_subprojects: bool,
        state: Optional[EntityVisibility] = None,
        name: str = None,
    ) -> Sequence[dict]:
        """
        Get list of unique parent tasks sorted by task name for the passed company projects
        If projects is None or empty then get parents for all the company tasks
        """
        query = cls._get_project_query(company_id, projects, include_subprojects=include_subprojects, state=state)

        parent_ids = set(Task.objects(query).distinct("parent"))
        if not parent_ids:
            return []

        parents: Sequence[dict] = Task.get_many_with_join(
            company_id,
            query=Q(id__in=parent_ids),
            query_dict={"name": name} if name else None,
            allow_public=True,
            override_projection=("id", "name", "project.name"),
        )

        return sorted(parents, key=itemgetter("name"))

    @classmethod
    def get_entity_users(
        cls,
        company: str,
        entity_cls: Type[Union[Task, Model]],
        projects: Sequence[str],
        include_subprojects: bool,
    ) -> Sequence[dict]:
        query = cls._get_project_query(company, projects, include_subprojects=include_subprojects)
        user_ids = entity_cls.objects(query).distinct(field="user")
        if not user_ids:
            return []
        users = UserProfile.objects(id__in=user_ids).only("id", "name")
        return [{"id": u.id, "name": u.name} for u in users]

    @classmethod
    def get_contributions(
        cls,
        company: str,
        project_id: str,
        period_days: int,
        utc_offset_minutes: int,
        limit: int,
        now: Optional[datetime] = None,
    ) -> dict:
        """
        Runs of the project (its tasks and its sub-projects' tasks, hidden tasks left out) created in the period,
        grouped by day and by the user who owns each run.
        The period is the last `period_days` local days, today included. Local days follow `utc_offset_minutes`
        (minutes east of UTC), so that a run is counted on the day the user saw it on.
        """
        period_start = contributions_period_start(now or datetime.utcnow(), utc_offset_minutes, period_days)
        pipeline = contributions_pipeline(
            company,
            project_ids=_ids_with_children([project_id]),
            period_start=period_start,
            utc_offset_minutes=utc_offset_minutes,
        )
        result = next(iter(Task.aggregate(pipeline)), {})
        names = {
            u.id: u.name
            for u in UserProfile.objects(id__in=contributor_ids(result, limit)).only("id", "name")
        }
        return contributions_response(period_days, result, names, limit)

    @classmethod
    def get_task_types(cls, company, project_ids: Optional[Sequence]) -> set:
        """
        Return the list of unique task types used by company and public tasks
        If project ids passed then only tasks from these projects are considered
        """
        query = cls._get_project_query(company, project_ids)
        res = Task.objects(query).distinct(field="type")
        return set(res).intersection(external_task_types)

    @classmethod
    def get_model_frameworks(cls, company, project_ids: Optional[Sequence]) -> Sequence:
        """
        Return the list of unique frameworks used by company and public models
        If project ids passed then only models from these projects are considered
        """
        query = cls._get_project_query(company, project_ids)
        return Model.objects(query).distinct(field="framework")

    @classmethod
    def calc_own_datasets(
        cls,
        company: str,
        project_ids: Sequence[str],
        filter_: Mapping[str, Any] = None,
        users: Sequence[str] = None,
    ) -> Dict[str, dict]:
        """
        Returns the amount of datasets per requested project
        """
        if not project_ids:
            return {}

        match = match_conditions(
            company=company,
            project_ids=project_ids,
            filter_=with_system_tag(filter_, dataset_tag),
            users=users,
            project_field="parent",
        )
        rows = Project.aggregate(count_by_field_pipeline(match, field="parent"))
        return own_count_by_project(project_ids, counts_by_project(rows), name="own_datasets")

    @classmethod
    def calc_own_contents(
        cls,
        company: str,
        project_ids: Sequence[str],
        filter_: Mapping[str, Any] = None,
        users: Sequence[str] = None,
    ) -> Dict[str, dict]:
        """
        Returns the amount of task/models per requested project
        Use separate aggregation calls on Task/Model instead of lookup
        aggregation on projects in order not to hit memory limits on large tasks
        """
        if not project_ids:
            return {}

        match = match_conditions(
            company=company,
            project_ids=project_ids,
            filter_=with_non_archived(filter_),
            users=users,
        )
        pipeline = count_by_field_pipeline(match, field="project")
        tasks = counts_by_project(Task.aggregate(pipeline))
        models = counts_by_project(Model.aggregate(pipeline))
        return own_contents_by_project(project_ids, tasks, models)
