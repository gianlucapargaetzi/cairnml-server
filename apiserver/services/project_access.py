from datetime import datetime

from apiserver.apierrors import errors
from apiserver.apimodels.base import UpdateResponse
from apiserver.apimodels.project_access import (
    AddMemberRequest,
    AddQueuesRequest,
    GetAssignableQueuesRequest,
    GetMembersRequest,
    GetQueuesRequest,
    RemoveMemberRequest,
    RemoveQueueRequest,
    SetVisibilityRequest,
    TransferOwnershipRequest,
    UpdateMemberRoleRequest,
)
from apiserver.bll.project.access import ProjectAccessBLL
from apiserver.bll.queue.queue_access import QueueAccessBLL
from apiserver.database.model.auth import User
from apiserver.database.model.queue import Queue
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.database.utils import get_options
from apiserver.service_repo import APICall, endpoint


def _resolve_root_id(call: APICall, project_id: str) -> str:
    root_id = ProjectAccessBLL.resolve_root_id(project_id, call.identity.company)
    if not root_id:
        raise errors.bad_request.InvalidProjectId(id=project_id)
    return root_id


def _load_root(company_id: str, root_id: str) -> Project:
    root = Project.objects(id=root_id, company=company_id).first()
    if not root:
        raise errors.bad_request.InvalidProjectId(id=root_id)
    return root


def _check_role(role: str) -> None:
    if role not in get_options(ProjectRole):
        raise errors.bad_request.FieldsValueError(role=role)


def _assert_owner(call: APICall, root: Project) -> None:
    """
    Queue allocations are changed by the owner only. Admin status does not count: the owner must also be allowed
    to use the queues they allocate.
    """
    if root.user != call.identity.user:
        raise errors.forbidden.NoWritePermission(
            "only the project owner can change the queues of the project"
        )


def _queue_summaries(company_id: str, queue_ids) -> list:
    queues = Queue.objects(company=company_id, id__in=list(queue_ids)).only("id", "name", "public")
    by_id = {q.id: q for q in queues}
    return [by_id[qid] for qid in queue_ids if qid in by_id]


def _find_member(root: Project, user_id: str):
    for member in root.members:
        if member.user == user_id:
            return member
    raise errors.bad_request.InvalidUserId(id=user_id)


@endpoint("projects.get_members")
def get_members(call: APICall, company_id: str, request: GetMembersRequest):
    root_id = _resolve_root_id(call, request.project)
    ProjectAccessBLL.assert_can(call.identity, request.project, "read")

    root = _load_root(company_id, root_id)
    call.result.data = {
        "owner": root.user,
        "members": [
            {"user": m.user, "role": m.role, "added": m.added, "added_by": m.added_by}
            for m in root.members
        ],
    }


@endpoint("projects.add_member", response_data_model=UpdateResponse)
def add_member(call: APICall, company_id: str, request: AddMemberRequest):
    root_id = _resolve_root_id(call, request.project)
    ProjectAccessBLL.assert_can(call.identity, request.project, "admin")
    _check_role(request.role)

    if not User.objects(id=request.user, company=company_id).only("id").first():
        raise errors.bad_request.InvalidUserId(id=request.user)

    root = _load_root(company_id, root_id)
    if request.user == root.user:
        # The Owner is never a member row; adding them would create one.
        raise errors.bad_request.InvalidUserId(id=request.user)

    existing = next((m for m in root.members if m.user == request.user), None)
    if existing:
        existing.role = request.role
        root.save()
    else:
        Project.objects(id=root_id, company=company_id).update_one(
            push__members=ProjectMember(
                user=request.user,
                role=request.role,
                added=datetime.utcnow(),
                added_by=call.identity.user,
            )
        )

    return UpdateResponse(updated=1)


@endpoint("projects.update_member_role", response_data_model=UpdateResponse)
def update_member_role(call: APICall, company_id: str, request: UpdateMemberRoleRequest):
    root_id = _resolve_root_id(call, request.project)
    ProjectAccessBLL.assert_can(call.identity, request.project, "admin")
    _check_role(request.role)

    root = _load_root(company_id, root_id)
    _find_member(root, request.user).role = request.role
    root.save()

    return UpdateResponse(updated=1)


@endpoint("projects.remove_member", response_data_model=UpdateResponse)
def remove_member(call: APICall, company_id: str, request: RemoveMemberRequest):
    root_id = _resolve_root_id(call, request.project)
    ProjectAccessBLL.assert_can(call.identity, request.project, "admin")

    _find_member(_load_root(company_id, root_id), request.user)
    Project.objects(id=root_id, company=company_id).update_one(
        pull__members__user=request.user
    )

    return UpdateResponse(updated=1)


@endpoint("projects.set_visibility", response_data_model=UpdateResponse)
def set_visibility(call: APICall, company_id: str, request: SetVisibilityRequest):
    root_id = _resolve_root_id(call, request.project)
    ProjectAccessBLL.assert_can(call.identity, request.project, "admin")
    if request.visibility not in get_options(ProjectVisibility):
        raise errors.bad_request.FieldsValueError(visibility=request.visibility)

    Project.objects(id=root_id, company=company_id).update_one(
        set__visibility=request.visibility
    )

    return UpdateResponse(updated=1)


@endpoint("projects.transfer_ownership", response_data_model=UpdateResponse)
def transfer_ownership(call: APICall, company_id: str, request: TransferOwnershipRequest):
    root_id = _resolve_root_id(call, request.project)
    ProjectAccessBLL.assert_can(call.identity, request.project, "owner_only")

    root = _load_root(company_id, root_id)
    # Precondition, not a capability: the new Owner must already be a member.
    _find_member(root, request.new_owner)

    now = datetime.utcnow()
    members = [
        m for m in root.members if m.user not in (request.new_owner, root.user)
    ]
    members.append(
        ProjectMember(
            user=root.user,
            role=ProjectRole.collaborator,
            added=now,
            added_by=call.identity.user,
        )
    )
    root.members = members
    root.user = request.new_owner
    # The new owner may allocate only the queues they may use without admin rights; the others are dropped
    assignable = QueueAccessBLL.assignable_queue_ids(company_id, request.new_owner)
    root.queues = [queue_id for queue_id in root.queues if queue_id in assignable]
    root.save()

    return UpdateResponse(updated=1)


@endpoint("projects.get_queues")
def get_queues(call: APICall, company_id: str, request: GetQueuesRequest):
    root_id = _resolve_root_id(call, request.project)
    ProjectAccessBLL.assert_can(call.identity, request.project, "read")

    root = _load_root(company_id, root_id)
    call.result.data = {
        "queues": [
            {"id": q.id, "name": q.name}
            for q in _queue_summaries(company_id, root.queues)
        ]
    }


@endpoint("projects.get_assignable_queues")
def get_assignable_queues(
    call: APICall, company_id: str, request: GetAssignableQueuesRequest
):
    root_id = _resolve_root_id(call, request.project)
    root = _load_root(company_id, root_id)
    _assert_owner(call, root)

    assignable = QueueAccessBLL.assignable_queue_ids(company_id, call.identity.user)
    call.result.data = {
        "queues": [
            {"id": q.id, "name": q.name, "public": bool(q.public)}
            for q in _queue_summaries(company_id, sorted(assignable))
        ]
    }


@endpoint("projects.add_queues", response_data_model=UpdateResponse)
def add_queues(call: APICall, company_id: str, request: AddQueuesRequest):
    root_id = _resolve_root_id(call, request.project)
    root = _load_root(company_id, root_id)
    _assert_owner(call, root)

    if len(set(request.queues)) != len(request.queues):
        raise errors.bad_request.ValidationError("a queue is listed more than once")

    already = set(root.queues)
    if already.intersection(request.queues):
        raise errors.bad_request.ValidationError(
            "a queue is already allocated to the project"
        )

    assignable = QueueAccessBLL.assignable_queue_ids(company_id, call.identity.user)
    for queue_id in request.queues:
        if queue_id not in assignable:
            raise errors.bad_request.InvalidQueueId(id=queue_id)

    if not request.queues:
        return UpdateResponse(updated=0)

    Project.objects(id=root_id, company=company_id).update_one(
        push_all__queues=list(request.queues)
    )
    return UpdateResponse(updated=1)


@endpoint("projects.remove_queue", response_data_model=UpdateResponse)
def remove_queue(call: APICall, company_id: str, request: RemoveQueueRequest):
    root_id = _resolve_root_id(call, request.project)
    root = _load_root(company_id, root_id)
    _assert_owner(call, root)

    if request.queue not in root.queues:
        raise errors.bad_request.InvalidQueueId(id=request.queue)

    Project.objects(id=root_id, company=company_id).update_one(
        pull__queues=request.queue
    )
    return UpdateResponse(updated=1)

