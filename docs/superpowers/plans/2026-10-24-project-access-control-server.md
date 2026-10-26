# Project Access Control (cairnml-server) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-project membership, roles (Owner/Collaborator/Read-Write/Read) and public/private visibility to `cairnml-server`, enforced on every endpoint that reads or writes a project, task or model.

**Architecture:** One new BLL module (`ProjectAccessBLL`) is the single place that decides "can this identity do this to this project." Every other change either feeds that module (the data model) or calls it (the enforcement sweeps across `projects.py`, `tasks.py`, `models.py`, plus the new member/visibility/ownership endpoints). The web UI is a separate, later plan — this plan ships a complete, independently testable API.

**Tech Stack:** Python 3.11, Flask-style `@endpoint` services, MongoEngine (`Document`/`EmbeddedDocument`), `jsonmodels` request models, pytest.

**Spec:** `docs/superpowers/specs/2026-10-24-project-access-control-design.md`

## Global Constraints

- Default visibility for a new project: `private` (spec goal 3).
- A private project is **fully hidden** from anyone with no role on it: same response as a genuinely missing id, never a distinguishable 403 (spec goal 4, anti-goals). This plan uses the exact existing error `errors.bad_request.InvalidProjectId(id=project_id)` for that case — do not introduce a new error class or a real HTTP 404.
- Pushing/creating a project whose name collides with one that exists but is invisible to the caller always creates a **new** project owned by the caller. It never attaches to, reveals, or merges with the invisible one (spec goal 4, addendum).
- Sub-projects have no membership or visibility of their own; every check resolves to the top-most ancestor (`Project.path[0]`, or the project's own id if `path` is empty) (spec goal 5).
- Company `admin` (`Role.admin`, from `apiserver/database/model/auth.py`) bypasses every check, on every project, with no membership row (spec goal 6).
- Ownership transfer: only the current Owner or an admin may call it; the target must already be a member (any role); on success the outgoing Owner is added back as a `collaborator` member (spec decisions).
- An ownerless project (Owner's user id no longer resolves) stays ownerless; nothing in this plan auto-assigns a new Owner.
- Moving a project: demoting a root to a sub-project clears its `visibility`/`members`; promoting a project to root gives it fresh defaults (`private`, no members), never a resurrected prior state (spec, left-to-builder section).
- Do not touch `projects.make_public`/`make_private`, `tasks.make_public`/`make_private`, `models.make_public`/`make_private`. These are a pre-existing, unrelated upstream ClearML feature (cross-company sharing via an empty `company` field) and use the word "public" for something else entirely. The new field is named `visibility`, never `public`/`is_public`.
- No data migration. The spec confirms there is no existing data; every project created from the moment this ships gets the rules above.

## Scope note added during execution (Task 6)

`reports.py`'s own endpoints (`reports.move`, `reports.create`) were never given their own `ProjectAccessBLL.assert_can` checks anywhere in Tasks 1-8 — only `projects.py`, `tasks.py` and `models.py` got a full enforcement sweep. This was an implicit, undocumented scoping choice in the original plan, not a reviewed decision. It is now closed: `reports.create` requires write on the named project, and `reports.move` requires admin on both sides, as `tasks.move` does. See the spec's non-goals section. `ProjectBLL.find_or_create` (used by `reports.py` too) was hardened to require write access to whatever project it resolves, as a defense-in-depth measure, but `reports.py`'s endpoints still have no project-level checks of their own. A full fix needs its own plan.

## Review Focus

- **A batch endpoint where the caller has rights to some ids and not others** (e.g. `tasks.delete_many` with a mix of their own and someone else's tasks). The matrix says nothing about partial batches; this plan picks all-or-nothing (the whole call is refused if any one id fails the check) and Task 6/7 must test exactly this — a batch that is entirely allowed, and a batch that is one id short of entirely allowed.
- **A project move that would let someone bypass the hidden-private rule** — moving a project they don't own under a parent they don't own, or vice versa. Task 5 pins both source and destination must pass an `admin`-capability check before a move proceeds.
- **A read-only (or non-member-on-public) user calling a write endpoint directly**, not just being denied in the UI. Every sweep task's tests must include a plain `read`-role or no-role call against at least one endpoint in its file, not only `read_write`-vs-`collaborator` cases.
- **The visibility-aware name lookup silently falling back to the old global-uniqueness behavior** for any one of the several call sites that resolve a project by name (`ProjectBLL.create`, `_ensure_project`, and the `move` endpoint's own call into `_ensure_project` for the destination path). Task 3's tests must cover both a direct `ProjectBLL.create` call and a nested-name one (`"Baz/Sub"`) that forks an invisible ancestor, not only a flat top-level name.
- **An admin's actions being blocked because the code checks membership before checking `Role.admin`.** Every sweep task's tests must include an admin call against a private project the admin was never added to.

---

### Task 1: Data model — roles, members, visibility

**Files:**
- Modify: `apiserver/database/model/project.py`
- Test: `apiserver/tests/database/test_project_access_model.py` (new)

**Interfaces:**
- Produces: `ProjectRole` (class with string constants `collaborator = "collaborator"`, `read_write = "read_write"`, `read = "read"`, and `get_options()` via the existing `get_options` helper from `apiserver/database/utils.py`, same pattern as `Role` in `apiserver/database/model/auth.py`); `ProjectVisibility` (class with `public = "public"`, `private = "private"`); `ProjectMember` (`EmbeddedDocument` with fields `user: StringField(required=True)`, `role: StringField(required=True, choices=get_options(ProjectRole))`, `added: DateTimeField(required=True)`, `added_by: StringField(required=True)`); `Project.visibility: StringField(choices=get_options(ProjectVisibility), default=ProjectVisibility.private)`; `Project.members: EmbeddedDocumentListField(ProjectMember, default=list)`.

- [ ] **Step 1: Write the failing test**

```python
# apiserver/tests/database/test_project_access_model.py
from datetime import datetime
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility

def test_new_project_defaults_to_private_with_no_members():
    project = Project(id="p1", company="c1", user="u1", name="Foo", basename="Foo", created=datetime.utcnow())
    assert project.visibility == ProjectVisibility.private
    assert project.members == []

def test_a_member_can_be_appended_and_read_back():
    project = Project(id="p1", company="c1", user="u1", name="Foo", basename="Foo", created=datetime.utcnow())
    project.members.append(ProjectMember(user="u2", role=ProjectRole.collaborator, added=datetime.utcnow(), added_by="u1"))
    assert project.members[0].user == "u2"
    assert project.members[0].role == ProjectRole.collaborator

def test_two_projects_with_the_same_name_no_longer_collide_at_the_field_level():
    # name no longer declares unique_with; this just documents the field has no such constraint object
    assert "unique_with" not in repr(Project.name)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest apiserver/tests/database/test_project_access_model.py -v`
Expected: FAIL with `ImportError: cannot import name 'ProjectMember'`

- [ ] **Step 3: Implement in `apiserver/database/model/project.py`**

Add near the top of the file, after the existing imports (add `EmbeddedDocument`, `EmbeddedDocumentListField` to the `mongoengine` import line, and `get_options` to the existing import from `apiserver.database.utils`):

```python
class ProjectRole(object):
    collaborator = "collaborator"
    read_write = "read_write"
    read = "read"


class ProjectVisibility(object):
    public = "public"
    private = "private"


class ProjectMember(EmbeddedDocument):
    meta = {"strict": False}
    user = StringField(required=True)
    role = StringField(required=True, choices=get_options(ProjectRole))
    added = DateTimeField(required=True)
    added_by = StringField(required=True)
```

On the `Project` class, add two fields (anywhere among the other field declarations):

```python
    visibility = StringField(choices=get_options(ProjectVisibility), default=ProjectVisibility.private)
    members = EmbeddedDocumentListField(ProjectMember, default=list)
```

On the existing `name` field, remove `unique_with=AttributedDocument.company.name,` (keep every other argument — `required=True`, `min_length=min_name_length`, `sparse=True`). This is the only line to delete in this task.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest apiserver/tests/database/test_project_access_model.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apiserver/database/model/project.py apiserver/tests/database/test_project_access_model.py
git commit -m "Add project roles, members and visibility to the Project model"
```

---

### Task 2: Core access decision — `ProjectAccessBLL`

**Files:**
- Create: `apiserver/bll/project/access.py`
- Test: `apiserver/tests/bll/project/test_access.py` (new)

**Interfaces:**
- Consumes: `Project`, `ProjectRole`, `ProjectVisibility` from Task 1. `apiserver.database.model.auth.Role` (existing). `apiserver.service_repo.auth.Identity` (existing: has `.user`, `.company`, `.role`). `apiserver.apierrors.errors` (existing: `bad_request.InvalidProjectId`, `forbidden.NoWritePermission`).
- Produces (used by every later task):
  - `ProjectAccessBLL.resolve_root_id(project_id: str, company: str) -> Optional[str]` — returns the top-most ancestor's id for the given project, or `None` if the project doesn't exist in that company.
  - `ProjectAccessBLL.get_role(identity: Identity, project_id: str) -> Optional[str]` — one of `"admin"`, `"owner"`, `ProjectRole.collaborator`, `ProjectRole.read_write`, `ProjectRole.read`, or `None`.
  - `ProjectAccessBLL.assert_can(identity: Identity, project_id: str, capability: str, object_owner: Optional[str] = None) -> None` — `capability` is one of `"read"`, `"write"`, `"delete"`, `"admin"`, `"owner_only"`. Raises `errors.bad_request.InvalidProjectId(id=project_id)` if the identity has no role at all, `errors.forbidden.NoWritePermission(...)` if it has a role but not enough for `capability`, otherwise returns `None`.

The five capabilities map exactly to the design doc's role matrix:

| capability | owner | collaborator | read_write | read / no role on a public project |
|---|---|---|---|---|
| `read` | ✓ | ✓ | ✓ | ✓ |
| `write` (create, edit any, enqueue/dequeue/abort) | ✓ | ✓ | only if `object_owner == identity.user` | ✗ |
| `delete` (delete any) | ✓ | only if `object_owner == identity.user` | only if `object_owner == identity.user` | ✗ |
| `admin` (project settings, members, visibility) | ✓ | ✓ | ✗ | ✗ |
| `owner_only` (transfer ownership, delete project) | ✓ | ✗ | ✗ | ✗ |

`admin` (the company role) always passes every capability, on every project, without a membership row.

- [ ] **Step 1: Write the failing test**

```python
# apiserver/tests/bll/project/test_access.py
from datetime import datetime
import pytest
from apiserver.apierrors import errors
from apiserver.bll.project.access import ProjectAccessBLL
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.service_repo.auth import Identity

COMPANY = "c1"


def make_root(visibility=ProjectVisibility.private, members=()):
    project = Project(
        id="root1", company=COMPANY, user="owner1", name="Root",
        basename="Root", created=datetime.utcnow(), visibility=visibility,
        members=[ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by="owner1") for u, r in members],
    )
    project.save()
    return project


def make_sub(root):
    sub = Project(
        id="sub1", company=COMPANY, user="someone", name="Root/Sub", basename="Sub",
        created=datetime.utcnow(), parent=root.id, path=[root.id],
    )
    sub.save()
    return sub


def identity(user, role=Role.user):
    return Identity(user=user, company=COMPANY, role=role)


class TestGetRole:
    def test_admin_is_admin_with_no_membership_row(self):
        make_root()
        assert ProjectAccessBLL.get_role(identity("stranger", role=Role.admin), "root1") == "admin"

    def test_owner_is_owner(self):
        make_root()
        assert ProjectAccessBLL.get_role(identity("owner1"), "root1") == "owner"

    def test_a_listed_member_gets_their_stored_role(self):
        make_root(members=[("u2", ProjectRole.collaborator)])
        assert ProjectAccessBLL.get_role(identity("u2"), "root1") == ProjectRole.collaborator

    def test_a_non_member_on_a_public_project_gets_implicit_read(self):
        make_root(visibility=ProjectVisibility.public)
        assert ProjectAccessBLL.get_role(identity("stranger"), "root1") == ProjectRole.read

    def test_a_non_member_on_a_private_project_gets_no_role(self):
        make_root(visibility=ProjectVisibility.private)
        assert ProjectAccessBLL.get_role(identity("stranger"), "root1") is None

    def test_a_sub_project_resolves_the_roots_role(self):
        root = make_root(members=[("u2", ProjectRole.read)])
        make_sub(root)
        assert ProjectAccessBLL.get_role(identity("u2"), "sub1") == ProjectRole.read

    def test_a_missing_project_id_gets_no_role(self):
        assert ProjectAccessBLL.get_role(identity("anyone"), "does-not-exist") is None

    def test_a_project_in_another_company_gets_no_role(self):
        make_root()
        other_company_identity = Identity(user="owner1", company="other-company", role=Role.user)
        assert ProjectAccessBLL.get_role(other_company_identity, "root1") is None


class TestAssertCan:
    def test_no_role_raises_invalid_project_id_not_forbidden(self):
        make_root(visibility=ProjectVisibility.private)
        with pytest.raises(errors.bad_request.InvalidProjectId):
            ProjectAccessBLL.assert_can(identity("stranger"), "root1", "read")

    def test_read_write_can_write_their_own_object(self):
        make_root(members=[("u2", ProjectRole.read_write)])
        ProjectAccessBLL.assert_can(identity("u2"), "root1", "write", object_owner="u2")

    def test_read_write_cannot_write_someone_elses_object(self):
        make_root(members=[("u2", ProjectRole.read_write)])
        with pytest.raises(errors.forbidden.NoWritePermission):
            ProjectAccessBLL.assert_can(identity("u2"), "root1", "write", object_owner="someone-else")

    def test_collaborator_can_write_someone_elses_object_but_not_delete_it(self):
        make_root(members=[("u2", ProjectRole.collaborator)])
        ProjectAccessBLL.assert_can(identity("u2"), "root1", "write", object_owner="someone-else")
        with pytest.raises(errors.forbidden.NoWritePermission):
            ProjectAccessBLL.assert_can(identity("u2"), "root1", "delete", object_owner="someone-else")

    def test_collaborator_can_manage_members_owner_only_actions_are_refused(self):
        make_root(members=[("u2", ProjectRole.collaborator)])
        ProjectAccessBLL.assert_can(identity("u2"), "root1", "admin")
        with pytest.raises(errors.forbidden.NoWritePermission):
            ProjectAccessBLL.assert_can(identity("u2"), "root1", "owner_only")

    def test_owner_passes_every_capability(self):
        make_root()
        for capability in ("read", "write", "delete", "admin", "owner_only"):
            ProjectAccessBLL.assert_can(identity("owner1"), "root1", capability, object_owner="anyone")

    def test_admin_passes_every_capability_on_a_private_project_with_no_membership(self):
        make_root(visibility=ProjectVisibility.private)
        for capability in ("read", "write", "delete", "admin", "owner_only"):
            ProjectAccessBLL.assert_can(identity("someone", role=Role.admin), "root1", capability, object_owner="anyone")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest apiserver/tests/bll/project/test_access.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'apiserver.bll.project.access'`

- [ ] **Step 3: Implement `apiserver/bll/project/access.py`**

```python
from typing import Optional

from apiserver.apierrors import errors
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectRole, ProjectVisibility
from apiserver.service_repo.auth import Identity

_WRITE_ROLES = {"owner", ProjectRole.collaborator}
_ADMIN_ROLES = {"owner", ProjectRole.collaborator}


class ProjectAccessBLL:
    @staticmethod
    def resolve_root_id(project_id: str, company: str) -> Optional[str]:
        project = Project.objects(id=project_id, company=company).only("id", "path").first()
        if not project:
            return None
        return project.path[0] if project.path else project.id

    @staticmethod
    def get_role(identity: Identity, project_id: str) -> Optional[str]:
        if identity.role == Role.admin:
            return "admin"

        root_id = ProjectAccessBLL.resolve_root_id(project_id, identity.company)
        if not root_id:
            return None
        root = Project.objects(id=root_id, company=identity.company).only(
            "user", "visibility", "members"
        ).first()
        if not root:
            return None

        if root.user == identity.user:
            return "owner"

        for member in root.members:
            if member.user == identity.user:
                return member.role

        if root.visibility == ProjectVisibility.public:
            return ProjectRole.read

        return None

    @staticmethod
    def assert_can(
        identity: Identity,
        project_id: str,
        capability: str,
        object_owner: Optional[str] = None,
    ) -> None:
        role = ProjectAccessBLL.get_role(identity, project_id)
        if role is None:
            raise errors.bad_request.InvalidProjectId(id=project_id)
        if role in ("admin", "owner"):
            return

        is_own = object_owner is not None and object_owner == identity.user

        if capability == "read":
            return
        if capability == "owner_only":
            pass
        elif capability == "admin":
            if role in _ADMIN_ROLES:
                return
        elif capability == "write":
            if role == ProjectRole.collaborator or (role == ProjectRole.read_write and is_own):
                return
        elif capability == "delete":
            if role in (ProjectRole.collaborator, ProjectRole.read_write) and is_own:
                return
        else:
            raise ValueError(f"unknown capability: {capability}")

        raise errors.forbidden.NoWritePermission(
            f"role '{role}' does not have '{capability}' access to project {project_id}"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest apiserver/tests/bll/project/test_access.py -v`
Expected: PASS (17 tests)

- [ ] **Step 5: Commit**

```bash
git add apiserver/bll/project/access.py apiserver/tests/bll/project/test_access.py
git commit -m "Add ProjectAccessBLL: the single place that decides project access"
```

---

### Task 3: Visibility-aware project name lookup (the hidden-collision fork)

**Files:**
- Modify: `apiserver/bll/project/sub_projects.py:95-112` (`_get_writable_project_from_name`)
- Modify: `apiserver/bll/project/project_bll.py` (every call site of `_get_writable_project_from_name` and `_ensure_project`)
- Test: `apiserver/tests/bll/project/test_visibility_aware_lookup.py` (new)

**Interfaces:**
- Consumes: `ProjectAccessBLL.get_role` from Task 2.
- Produces: `_get_writable_project_from_name(company, name, identity, _only=...)` — same as today, with one added required parameter `identity: Identity`, placed third positionally. Returns the existing project only if `ProjectAccessBLL.get_role(identity, project.id)` is not `None`; otherwise returns `None` exactly as if the project didn't exist (so the caller creates a new one). `_ensure_project(company, user, name, identity, creation_params=None)` gains the same `identity` parameter, placed fourth positionally, passed straight through to `_get_writable_project_from_name`.

- [ ] **Step 1: Write the failing test**

```python
# apiserver/tests/bll/project/test_visibility_aware_lookup.py
from datetime import datetime
from apiserver.bll.project.project_bll import ProjectBLL
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectVisibility
from apiserver.service_repo.auth import Identity

COMPANY = "c1"


def identity(user):
    return Identity(user=user, company=COMPANY, role=Role.user)


def test_creating_a_project_with_a_name_that_collides_with_an_invisible_private_project_forks_a_new_one():
    Project(
        id="hidden1", company=COMPANY, user="owner1", name="Foo", basename="Foo",
        created=datetime.utcnow(), visibility=ProjectVisibility.private,
    ).save()

    new_id = ProjectBLL.create(user="owner2", company=COMPANY, name="Foo", identity=identity("owner2"))

    assert new_id != "hidden1"
    created = Project.objects(id=new_id).first()
    assert created.user == "owner2"
    assert created.name == "Foo"


def test_creating_a_project_with_a_name_that_collides_with_a_public_project_reuses_it():
    existing = Project(
        id="public1", company=COMPANY, user="owner1", name="Bar", basename="Bar",
        created=datetime.utcnow(), visibility=ProjectVisibility.public,
    )
    existing.save()

    found_id = ProjectBLL.create(user="owner2", company=COMPANY, name="Bar", identity=identity("owner2"))

    assert found_id == "public1"


def test_pushing_a_task_into_a_new_sub_project_path_still_forks_on_an_invisible_root():
    Project(
        id="hidden2", company=COMPANY, user="owner1", name="Baz", basename="Baz",
        created=datetime.utcnow(), visibility=ProjectVisibility.private,
    ).save()

    new_id = ProjectBLL.create(user="owner2", company=COMPANY, name="Baz/Sub", identity=identity("owner2"))

    created = Project.objects(id=new_id).first()
    assert created.parent != "hidden2"
    root = Project.objects(id=created.parent).first()
    assert root.user == "owner2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest apiserver/tests/bll/project/test_visibility_aware_lookup.py -v`
Expected: FAIL with `TypeError: create() got an unexpected keyword argument 'identity'`

- [ ] **Step 3: Implement**

In `apiserver/bll/project/sub_projects.py`, change the signature of `_get_writable_project_from_name` to accept `identity` as its third positional parameter (after `company`, `name`), keeping `_only` as a keyword-only default exactly as today. Immediately after the existing query returns its list of candidate projects and before picking one to return, filter out any candidate where `ProjectAccessBLL.get_role(identity, candidate.id) is None` (import `ProjectAccessBLL` from `apiserver.bll.project.access`). If no candidate survives the filter, return `None` (the existing "not found" path), which is exactly what makes the caller create a new project. Change `_ensure_project`'s signature to accept `identity` as its fourth positional parameter (after `company`, `user`, `name`) and pass it straight through to its own call to `_get_writable_project_from_name`, and to its recursive call to itself (for the parent).

In `apiserver/bll/project/project_bll.py`, `ProjectBLL.create` gains an `identity: Identity` parameter (add it to the signature; it is required, no default) and passes it to its call to `_get_writable_project_from_name` and to `_ensure_project`. Update every other caller of `_ensure_project` in this file (the `move` method, which already has an `identity`-bearing caller upstream — check `apiserver/services/projects.py`'s `move` endpoint and thread `call.identity` through) to pass `identity` the same way. Update `apiserver/services/projects.py`'s `create` endpoint (around line 286) to pass `identity=identity` (it already has `identity = call.identity` on the line above). Search the whole `apiserver/bll/project/` and `apiserver/services/` trees for any other caller of `ProjectBLL.create`, `_ensure_project`, or `_get_writable_project_from_name` (`grep -rn "ProjectBLL.create\|_ensure_project\|_get_writable_project_from_name" apiserver --include=*.py`) and thread `identity` through each.

Note for whoever implements this: task creation does **not** go through this code. `apiserver/bll/task/task_bll.py` receives an already-resolved project *id* (`Project.get_for_writing(company=..., id=task.project)`), never a name — the CairnML SDK itself resolves or creates the project client-side (by calling `projects.create`, which is exactly the path this task already fixes) before it ever calls `tasks.create`. So there is no second fork-on-collision path to patch here; the project-creation fix in this task is the only place the "push a task into a colliding name" scenario can originate, and it's already covered.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest apiserver/tests/bll/project/test_visibility_aware_lookup.py -v`
Expected: PASS

- [ ] **Step 5: Run the full existing project test suite to confirm nothing else broke**

Run: `python -m pytest apiserver/tests/bll/project -v`
Expected: PASS (every pre-existing project test, plus the new ones)

- [ ] **Step 6: Commit**

```bash
git add apiserver/bll/project/sub_projects.py apiserver/bll/project/project_bll.py apiserver/bll/task/task_bll.py apiserver/services/projects.py apiserver/tests/bll/project/test_visibility_aware_lookup.py
git commit -m "Scope project name uniqueness to what the caller can see"
```

---

### Task 4: Member, visibility and ownership-transfer endpoints

**Files:**
- Create: `apiserver/apimodels/project_access.py`
- Create: `apiserver/services/project_access.py`
- Test: `apiserver/tests/services/test_project_access.py` (new)

**Interfaces:**
- Consumes: `ProjectAccessBLL` from Task 2, `ProjectMember`/`ProjectRole`/`ProjectVisibility` from Task 1.
- Produces endpoints (auto-discovered by `ServiceRepo` from any `.py` file under `apiserver/services/` — no registry to update): `projects.get_members`, `projects.add_member`, `projects.update_member_role`, `projects.remove_member`, `projects.set_visibility`, `projects.transfer_ownership`.

- [ ] **Step 1: Write the failing test**

Every function below takes the same three arguments every other endpoint in this codebase takes — `(call: APICall, company_id: str, request: <RequestModel>)` — never a single bundled object. The test's `FakeCall` therefore carries only `.identity`; every field the endpoint needs comes from the real request object passed as the third argument, exactly like the `DeleteRequest`/`MoveRequest` instances Tasks 5–7 construct directly.

```python
# apiserver/tests/services/test_project_access.py
from datetime import datetime
import pytest
from apiserver.apierrors import errors
from apiserver.apimodels.project_access import (
    AddMemberRequest, GetMembersRequest, RemoveMemberRequest,
    SetVisibilityRequest, TransferOwnershipRequest, UpdateMemberRoleRequest,
)
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.service_repo.auth import Identity
from apiserver.services import project_access as svc

COMPANY = "c1"


def make_root(visibility=ProjectVisibility.private, members=()):
    project = Project(
        id="root1", company=COMPANY, user="owner1", name="Root", basename="Root",
        created=datetime.utcnow(), visibility=visibility,
        members=[ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by="owner1") for u, r in members],
    )
    project.save()
    return project


def identity(user, role=Role.user):
    return Identity(user=user, company=COMPANY, role=role)


class FakeCall:
    def __init__(self, ident):
        self.identity = ident


def test_owner_can_add_a_member():
    make_root()
    svc.add_member(FakeCall(identity("owner1")), COMPANY, AddMemberRequest(project="root1", user="u2", role=ProjectRole.read))
    assert Project.objects(id="root1").first().members[0].user == "u2"


def test_read_write_cannot_add_a_member():
    make_root(members=[("u2", ProjectRole.read_write)])
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.add_member(FakeCall(identity("u2")), COMPANY, AddMemberRequest(project="root1", user="u3", role=ProjectRole.read))


def test_owner_can_change_a_members_role():
    make_root(members=[("u2", ProjectRole.read)])
    svc.update_member_role(FakeCall(identity("owner1")), COMPANY, UpdateMemberRoleRequest(project="root1", user="u2", role=ProjectRole.collaborator))
    assert Project.objects(id="root1").first().members[0].role == ProjectRole.collaborator


def test_owner_can_remove_a_member():
    make_root(members=[("u2", ProjectRole.read)])
    svc.remove_member(FakeCall(identity("owner1")), COMPANY, RemoveMemberRequest(project="root1", user="u2"))
    assert Project.objects(id="root1").first().members == []


def test_owner_can_set_visibility():
    make_root(visibility=ProjectVisibility.private)
    svc.set_visibility(FakeCall(identity("owner1")), COMPANY, SetVisibilityRequest(project="root1", visibility=ProjectVisibility.public))
    assert Project.objects(id="root1").first().visibility == ProjectVisibility.public


def test_transfer_requires_the_target_to_already_be_a_member():
    make_root()
    with pytest.raises(errors.bad_request.InvalidUserId):
        svc.transfer_ownership(FakeCall(identity("owner1")), COMPANY, TransferOwnershipRequest(project="root1", new_owner="stranger"))


def test_the_owner_is_not_a_member_row_so_cannot_be_targeted_by_member_endpoints():
    # Structural guarantee, not just a policy check: the Owner was never appended to `members`
    # (see Task 1/4's data model), so there is no member row for update_member_role/remove_member
    # to find when given the Owner's own user id — this is exactly the anti-goal "a Collaborator
    # manages to strip the Owner's role."
    make_root(members=[("u2", ProjectRole.collaborator)])
    with pytest.raises(errors.bad_request.InvalidUserId):
        svc.update_member_role(FakeCall(identity("u2")), COMPANY, UpdateMemberRoleRequest(project="root1", user="owner1", role=ProjectRole.read))
    with pytest.raises(errors.bad_request.InvalidUserId):
        svc.remove_member(FakeCall(identity("owner1")), COMPANY, RemoveMemberRequest(project="root1", user="owner1"))


def test_transfer_makes_the_target_owner_and_keeps_the_old_owner_as_a_collaborator():
    make_root(members=[("u2", ProjectRole.read)])
    svc.transfer_ownership(FakeCall(identity("owner1")), COMPANY, TransferOwnershipRequest(project="root1", new_owner="u2"))
    project = Project.objects(id="root1").first()
    assert project.user == "u2"
    members_by_user = {m.user: m.role for m in project.members}
    assert "u2" not in members_by_user
    assert members_by_user["owner1"] == ProjectRole.collaborator


def test_only_owner_or_admin_can_transfer():
    make_root(members=[("u2", ProjectRole.collaborator), ("u3", ProjectRole.read)])
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.transfer_ownership(FakeCall(identity("u2")), COMPANY, TransferOwnershipRequest(project="root1", new_owner="u3"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest apiserver/tests/services/test_project_access.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'apiserver.services.project_access'`

- [ ] **Step 3: Implement `apiserver/apimodels/project_access.py`**

```python
from jsonmodels import models, fields


class ProjectMemberRequest(models.Base):
    project = fields.StringField(required=True)
    user = fields.StringField(required=True)


class AddMemberRequest(ProjectMemberRequest):
    role = fields.StringField(required=True)


class UpdateMemberRoleRequest(ProjectMemberRequest):
    role = fields.StringField(required=True)


class RemoveMemberRequest(ProjectMemberRequest):
    pass


class GetMembersRequest(models.Base):
    project = fields.StringField(required=True)


class SetVisibilityRequest(models.Base):
    project = fields.StringField(required=True)
    visibility = fields.StringField(required=True)


class TransferOwnershipRequest(models.Base):
    project = fields.StringField(required=True)
    new_owner = fields.StringField(required=True)
```

- [ ] **Step 4: Implement `apiserver/services/project_access.py`**

Follow the exact pattern already used in `apiserver/services/projects.py` (`@endpoint(...)` decorator, `call: APICall` first parameter, `call.identity` for the caller, `errors.bad_request.InvalidUserId` for a target user id that doesn't exist — check `apiserver/apierrors/errors/bad_request.py` for its exact field name before using it, and `apiserver/database/model/auth.py`'s `User` model to look the target user up). Six endpoints:

- `projects.get_members` (request `GetMembersRequest`): `ProjectAccessBLL.assert_can(call.identity, data.project, "read")`; resolve the root (`ProjectAccessBLL.resolve_root_id`), load it, set `call.result.data = {"owner": root.user, "members": [{"user": m.user, "role": m.role, "added": m.added, "added_by": m.added_by} for m in root.members]}`.
- `projects.add_member` (request `AddMemberRequest`, response `UpdateResponse` from `apiserver.apimodels.base`): `assert_can(..., "admin")`; verify `data.user` resolves to an existing `User` in the same company (raise `InvalidUserId` if not — check its real field name first); resolve the root; if `data.user` is already a member, update their role in place instead of appending a duplicate; else `Project.objects(id=root_id).update_one(push__members=ProjectMember(user=data.user, role=data.role, added=datetime.utcnow(), added_by=call.identity.user))`; return `UpdateResponse(updated=1)`.
- `projects.update_member_role` (request `UpdateMemberRoleRequest`, response `UpdateResponse`): `assert_can(..., "admin")`; load the root project as a full document (not `.only(...)`), find the member with `member.user == data.user` in the Python list, raise `InvalidUserId` if none, set `member.role = data.role`, call `.save()`; return `UpdateResponse(updated=1)`.
- `projects.remove_member` (request `RemoveMemberRequest`, response `UpdateResponse`): `assert_can(..., "admin")`; `Project.objects(id=root_id).update_one(pull__members__user=data.user)`; return `UpdateResponse(updated=1)`.
- `projects.set_visibility` (request `SetVisibilityRequest`, response `UpdateResponse`): `assert_can(..., "admin")`; validate `data.visibility` is one of `get_options(ProjectVisibility)` (raise a validation error otherwise, matching how other endpoints validate an enum-like string field — check `apiserver/services/projects.py` for the pattern it already uses, e.g. for `EntityVisibility`); `Project.objects(id=root_id).update_one(set__visibility=data.visibility)`; return `UpdateResponse(updated=1)`.
- `projects.transfer_ownership` (request `TransferOwnershipRequest`, response `UpdateResponse`): `assert_can(..., "owner_only")`; load the root as a full document; find `data.new_owner` in `root.members` and raise `errors.bad_request.InvalidUserId(...)` if absent (this is the "must already be a member" rule — it is not a capability check, it is a precondition, so it is checked here, not inside `assert_can`); remove that member from the list; append a new `ProjectMember` for the outgoing owner (`user=root.user`, `role=ProjectRole.collaborator`, `added=datetime.utcnow()`, `added_by=call.identity.user`); set `root.user = data.new_owner`; call `.save()`; return `UpdateResponse(updated=1)`.

Every endpoint resolves `root_id = ProjectAccessBLL.resolve_root_id(data.project, call.identity.company)` and raises `errors.bad_request.InvalidProjectId(id=data.project)` immediately if it is `None`, before calling `assert_can` (so a request naming a genuinely nonexistent project id fails the same way whether or not the caller would otherwise have had access — `assert_can` itself re-derives this when it calls `get_role`, so this is a belt-and-suspenders check: either order is correct, but resolve first so every endpoint has `root_id` on hand for its own logic).

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest apiserver/tests/services/test_project_access.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add apiserver/apimodels/project_access.py apiserver/services/project_access.py apiserver/tests/services/test_project_access.py
git commit -m "Add member, visibility and ownership-transfer endpoints"
```

---

### Task 5: Enforce access in `projects.py`

**Files:**
- Modify: `apiserver/services/projects.py`
- Modify: `apiserver/bll/project/access.py` (adding `visible_project_ids`, `visible_projects_query`, `reset_access` to the class Task 2 created — do not recreate the file)
- Test: `apiserver/tests/services/test_projects_access_enforcement.py` (new)

**Interfaces:**
- Consumes: `ProjectAccessBLL` from Task 2.
- Produces (added to the same `apiserver/bll/project/access.py` file from Task 2): `ProjectAccessBLL.visible_project_ids(identity: Identity) -> Optional[Set[str]]`, `ProjectAccessBLL.reset_access(project_id: str) -> None` (sets `visibility` back to `ProjectVisibility.private` and `members` to `[]` on the given project — used whenever a move changes whether a project is a root).

Classification of every endpoint in this file (do not touch `make_public`/`make_private` — unrelated, see Global Constraints):

| Endpoint | Capability | object_owner |
|---|---|---|
| `get_by_id`, `get_all_ex`, `get_all` | filter, not a per-call check — see below | — |
| `get_unique_metric_variants`, `get_model_metadata_keys`, `get_model_metadata_values`, `get_hyper_parameters`, `get_hyperparam_values`, `get_project_tags`, `get_task_tags`, `get_model_tags`, `get_task_parents`, `get_user_names` | `read` | — |
| `create` | n/a — creation itself has no access check; the creator becomes Owner by construction (Task 3 already handles the name-collision fork) | — |
| `update`, `move`, `merge` | `admin` on the project(s) named in the request; for `move` and `merge`, check `admin` on **both** the project being moved/merged **and** the destination/target project (two calls to `assert_can`) | — |
| `validate_delete`, `delete` | `owner_only` | — |

For the three `get_all*` endpoints: rather than checking each returned project individually (slow, and awkward to express as a MongoEngine query), add a method to `ProjectAccessBLL` in this task: `ProjectAccessBLL.visible_projects_query(identity: Identity) -> Q` returning a MongoEngine `Q` object that is `Q(user=identity.user) | Q(visibility=ProjectVisibility.public) | Q(members__user=identity.user)` — or, if `identity.role == Role.admin`, `Q()` (an empty filter, i.e. everything). `AND` this `Q` into the existing query in `get_by_id`, `get_all_ex`, `get_all` at the point they build their `Project.objects(...)` query (each already builds a query from `parameters`/`data` — find the exact line with `grep -n "Project.objects" apiserver/services/projects.py` and `AND` the new `Q` in, e.g. `Project.objects(existing_query & ProjectAccessBLL.visible_projects_query(call.identity))`). Note this filter only looks at **root** projects' `user`/`visibility`/`members`; a sub-project is visible exactly when its root is, so the filter must match on `Q(id__in=path) ...`-style logic is **not** needed here if every root project's descendants are reached by filtering on the resolved root instead — the straightforward correct approach is: compute the set of project ids the caller may see by running the `Q` filter above against **root projects only** (`Project.objects(parent=None).filter(q_above).only("id")`), then also include every project whose `path[0]` is in that set. Implement this as `ProjectAccessBLL.visible_project_ids(identity) -> Optional[Set[str]]` (returns `None` for admin, meaning "no filter needed"; otherwise the full set of visible ids, roots and descendants) and use `Project.objects(id__in=visible_ids)` (ANDed with the existing query) in the three list/get endpoints when the returned set is not `None`.

- [ ] **Step 1: Write the failing test**

```python
# apiserver/tests/services/test_projects_access_enforcement.py
from datetime import datetime
import pytest
from apiserver.apierrors import errors
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.service_repo.auth import Identity
from apiserver.bll.project.access import ProjectAccessBLL

COMPANY = "c1"


def make_root(visibility=ProjectVisibility.private, members=(), id_="root1", owner="owner1"):
    project = Project(
        id=id_, company=COMPANY, user=owner, name=f"Project-{id_}", basename=f"Project-{id_}",
        created=datetime.utcnow(), visibility=visibility,
        members=[ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by=owner) for u, r in members],
    )
    project.save()
    return project


def identity(user, role=Role.user):
    return Identity(user=user, company=COMPANY, role=role)


def test_a_stranger_cannot_see_a_private_project_in_the_visible_ids():
    make_root(visibility=ProjectVisibility.private, id_="priv1")
    ids = ProjectAccessBLL.visible_project_ids(identity("stranger"))
    assert "priv1" not in ids


def test_everyone_sees_a_public_project():
    make_root(visibility=ProjectVisibility.public, id_="pub1")
    ids = ProjectAccessBLL.visible_project_ids(identity("stranger"))
    assert "pub1" in ids


def test_admin_sees_everything_filter_is_none():
    make_root(visibility=ProjectVisibility.private, id_="priv2")
    assert ProjectAccessBLL.visible_project_ids(identity("someone", role=Role.admin)) is None


def test_owner_only_can_delete_a_project_collaborator_cannot():
    from apiserver.services.projects import delete as delete_endpoint
    make_root(members=[("u2", ProjectRole.collaborator)], id_="root2")

    class FakeCall:
        def __init__(self, ident, **data):
            self.identity = ident
            self.data = data

    with pytest.raises(errors.forbidden.NoWritePermission):
        delete_endpoint(FakeCall(identity("u2"), project="root2", force=True), COMPANY, type("R", (), {"project": "root2", "force": True, "delete_contents": False, "delete_external_artifacts": True})())


def test_a_read_role_cannot_update_project_settings():
    from apiserver.services.projects import update as update_endpoint
    make_root(members=[("u2", ProjectRole.read)], id_="root3")

    class FakeCall:
        def __init__(self, ident, **data):
            self.identity = ident
            self.data = data

    with pytest.raises(errors.forbidden.NoWritePermission):
        update_endpoint(FakeCall(identity("u2"), project="root3", name="New Name"), COMPANY, type("R", (), {"project": "root3", "name": "New Name"})())


def test_move_requires_admin_on_both_source_and_destination():
    from apiserver.services.projects import move as move_endpoint
    make_root(members=[("u2", ProjectRole.collaborator)], id_="src1")  # u2 is collaborator on the source
    make_root(members=[], id_="dst1", owner="someone-else")  # u2 has no role at all on the destination

    class FakeCall:
        def __init__(self, ident, **data):
            self.identity = ident
            self.data = data

    with pytest.raises(errors.forbidden.NoWritePermission):
        move_endpoint(FakeCall(identity("u2"), project="src1", new_location="Dest"), COMPANY, type("R", (), {"project": "src1", "new_location": "Dest"})())


def test_reset_access_clears_visibility_and_members():
    from apiserver.bll.project.access import ProjectAccessBLL
    root = make_root(visibility=ProjectVisibility.public, members=[("u2", ProjectRole.collaborator)], id_="root4")
    ProjectAccessBLL.reset_access(root.id)
    refreshed = Project.objects(id="root4").first()
    assert refreshed.visibility == ProjectVisibility.private
    assert refreshed.members == []
```

(The last two tests' `move_endpoint`/`update_endpoint` calls use a stand-in request object the same way the first test does — before trusting the exact field names, check `apiserver/apimodels/projects.py`'s `MoveRequest`/`UpdateRequest`-equivalent and prefer constructing the real request class directly, e.g. `MoveRequest(project="src1", new_location="Dest")`, over the `type("R", (), {...})()` stand-in, if its fields accept that construction outside of a real `APICall`.)

(Adjust the last test's request-object construction to match whatever `DeleteRequest` from `apiserver.apimodels.projects` actually needs when instantiated directly — read that class before writing this test, and prefer calling `DeleteRequest(project="root2", force=True)` if its fields all have defaults; only hand-build a stand-in object if that constructor call doesn't work with jsonmodels validation outside of a real APICall.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest apiserver/tests/services/test_projects_access_enforcement.py -v`
Expected: FAIL with `AttributeError: type object 'ProjectAccessBLL' has no attribute 'visible_project_ids'`

- [ ] **Step 3: Implement**

Add `visible_project_ids` and `visible_projects_query` to `apiserver/bll/project/access.py` (Task 2's file) as described above. Then, in `apiserver/services/projects.py`:
- In `get_by_id`, `get_all_ex`, `get_all`: compute `visible_ids = ProjectAccessBLL.visible_project_ids(call.identity)` once near the top; if not `None`, add `id__in=visible_ids` to the query being built (find each function's existing `Project.objects(...)` call or equivalent query-building helper and extend it).
- In `update`: before the update happens, call `ProjectAccessBLL.assert_can(call.identity, data.project, "admin")`.
- In `move`: call `assert_can(..., "admin")` on both `data.project` and `data.new_location`'s resolved destination project id (if the destination doesn't exist yet as a project, this check is skipped for it — only check an id that already resolves to a project). Then, after the move itself succeeds (the existing code already re-parents the project), re-read whether the moved project is now a root (`parent is None`) or a sub-project: if it became a sub-project (it was a root before and isn't anymore), clear its `visibility` back to the field's default and empty its `members` list (`Project.objects(id=data.project).update_one(set__visibility=ProjectVisibility.private, set__members=[])`); if it became a root (it was a sub-project before and isn't anymore — check this by comparing `parent`/`path` before and after the move, since `move`'s existing code already has both), ensure the same reset is applied so it starts with fresh defaults rather than whatever stale `visibility`/`members` the field happened to hold from before it was ever a root. In both directions the rule is identical — reset to defaults — so one helper (`ProjectAccessBLL.reset_access(project_id)`, doing exactly that one `update_one` call) covers it; call it whenever the moved project's root-ness changed, skip it when it didn't (a sub-project moved to a different sub-project location stays a sub-project and keeps nothing of its own to reset).
- In `merge`: call `assert_can(..., "admin")` on both `data.project` and `data.destination_project`.
- In `validate_delete` and `delete`: call `assert_can(..., "owner_only")`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest apiserver/tests/services/test_projects_access_enforcement.py -v`
Expected: PASS

- [ ] **Step 5: Run the full existing projects test suite to confirm nothing else broke**

Run: `python -m pytest apiserver/tests -k project -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add apiserver/bll/project/access.py apiserver/services/projects.py apiserver/tests/services/test_projects_access_enforcement.py
git commit -m "Enforce project access in projects.py"
```

---

### Task 6: Enforce access in `tasks.py`

**Files:**
- Modify: `apiserver/services/tasks.py`
- Test: `apiserver/tests/services/test_tasks_access_enforcement.py` (new)

**Interfaces:**
- Consumes: `ProjectAccessBLL` from Task 2 (and `visible_project_ids` from Task 5).

Every task-scoped endpoint resolves the task's `project` field (load the task, or for a batch of ids, load all of them) and calls `ProjectAccessBLL.assert_can(call.identity, task.project, capability, object_owner=task.user)`. Do not touch `tasks.make_public`/`make_private` (unrelated, see Global Constraints).

| Endpoint(s) | Capability | Notes |
|---|---|---|
| `get_by_id`, `get_all_ex`, `get_by_id_ex`, `get_all`, `get_types` | `read`, via the same `visible_project_ids` filter pattern as Task 5 (`AND id__in` on the task's `project` field, i.e. `project__in=visible_ids`) | — |
| `validate`, `create`, `clone` | `write` on the task's (or clone source's) project; `clone` additionally needs the *new* task's eventual project checked the same way once it's known | — |
| `update_batch`, `edit`, `set_requirements`, `edit_hyper_params`, `delete_hyper_params`, `edit_configuration`, `delete_configuration`, `update_tags`, `add_or_update_artifacts`, `delete_artifacts`, `add_or_update_model`, `delete_models`, `edit_runtime` | `write`, `object_owner=task.user` | — |
| `stop`, `stop_many`, `stopped`, `started`, `failed`, `close`, `enqueue`, `enqueue_many`, `dequeue`, `dequeue_many`, `reset`, `reset_many`, `archive`, `archive_many`, `unarchive_many`, `publish`, `publish_many`, `completed`, `ping` | `write`, `object_owner=task.user` | These are the "schedule jobs on workers" actions from the spec — same rule as edit |
| `delete`, `delete_many` | `delete`, `object_owner=task.user` | |
| `move` | `admin` on the task's current project and on the destination project (same pattern as `projects.move` in Task 5) | |

For every `*_many` endpoint (`stop_many`, `delete_many`, `enqueue_many`, `dequeue_many`, `reset_many`, `archive_many`, `unarchive_many`, `publish_many`): load every named task first, call `assert_can` for each one **before** performing any mutation on any of them (all-or-nothing — the first failing check raises and nothing in the batch is changed; do not catch-and-skip). This matches the Review Focus item on partial batches.

- [ ] **Step 1: Write the failing test**

```python
# apiserver/tests/services/test_tasks_access_enforcement.py
from datetime import datetime
import pytest
from apiserver.apierrors import errors
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.database.model.task.task import Task
from apiserver.service_repo.auth import Identity

COMPANY = "c1"


def make_root(visibility=ProjectVisibility.private, members=(), id_="root1", owner="owner1"):
    project = Project(
        id=id_, company=COMPANY, user=owner, name=f"Project-{id_}", basename=f"Project-{id_}",
        created=datetime.utcnow(), visibility=visibility,
        members=[ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by=owner) for u, r in members],
    )
    project.save()
    return project


def make_task(project_id, owner, id_="task1"):
    task = Task(id=id_, company=COMPANY, user=owner, project=project_id, name=f"Task-{id_}", type="training", created=datetime.utcnow())
    task.save()
    return task


def identity(user, role=Role.user):
    return Identity(user=user, company=COMPANY, role=role)


class FakeCall:
    def __init__(self, ident, **data):
        self.identity = ident
        self.data = data


def test_read_write_can_delete_their_own_task():
    make_root(members=[("u2", ProjectRole.read_write)])
    make_task("root1", "u2")
    from apiserver.services.tasks import delete as delete_endpoint
    from apiserver.apimodels.tasks import DeleteRequest
    delete_endpoint(FakeCall(identity("u2")), COMPANY, DeleteRequest(task="task1", force=True))
    assert Task.objects(id="task1").first() is None


def test_read_write_cannot_delete_someone_elses_task():
    make_root(members=[("u2", ProjectRole.read_write)])
    make_task("root1", "owner1")
    from apiserver.services.tasks import delete as delete_endpoint
    from apiserver.apimodels.tasks import DeleteRequest
    with pytest.raises(errors.forbidden.NoWritePermission):
        delete_endpoint(FakeCall(identity("u2")), COMPANY, DeleteRequest(task="task1", force=True))
    assert Task.objects(id="task1").first() is not None


def test_collaborator_can_edit_but_not_delete_someone_elses_task():
    make_root(members=[("u2", ProjectRole.collaborator)])
    make_task("root1", "owner1")
    from apiserver.services.tasks import delete as delete_endpoint
    from apiserver.apimodels.tasks import DeleteRequest
    with pytest.raises(errors.forbidden.NoWritePermission):
        delete_endpoint(FakeCall(identity("u2")), COMPANY, DeleteRequest(task="task1", force=True))


def test_a_read_role_cannot_enqueue_a_task():
    make_root(members=[("u2", ProjectRole.read)])
    make_task("root1", "owner1")
    from apiserver.services.tasks import enqueue as enqueue_endpoint
    from apiserver.apimodels.tasks import EnqueueRequest
    with pytest.raises(errors.forbidden.NoWritePermission):
        enqueue_endpoint(FakeCall(identity("u2")), COMPANY, EnqueueRequest(task="task1", queue="default"))


def test_delete_many_is_all_or_nothing():
    make_root(members=[("u2", ProjectRole.read_write)])
    make_task("root1", "u2", id_="own-task")
    make_task("root1", "owner1", id_="other-task")
    from apiserver.services.tasks import delete_many as delete_many_endpoint
    from apiserver.apimodels.tasks import DeleteManyRequest
    with pytest.raises(errors.forbidden.NoWritePermission):
        delete_many_endpoint(FakeCall(identity("u2")), COMPANY, DeleteManyRequest(ids=["own-task", "other-task"], force=True))
    assert Task.objects(id="own-task").first() is not None


def test_admin_can_delete_anyones_task_on_a_private_project_with_no_membership():
    make_root(visibility=ProjectVisibility.private)
    make_task("root1", "owner1")
    from apiserver.services.tasks import delete as delete_endpoint
    from apiserver.apimodels.tasks import DeleteRequest
    delete_endpoint(FakeCall(identity("admin-user", role=Role.admin)), COMPANY, DeleteRequest(task="task1", force=True))
    assert Task.objects(id="task1").first() is None
```

(Before relying on `DeleteRequest`, `EnqueueRequest`, `DeleteManyRequest` field names, check `apiserver/apimodels/tasks.py` — field names here are a best guess from the design's conventions, not verified against that file; fix the test's keyword arguments to match what actually exists there.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest apiserver/tests/services/test_tasks_access_enforcement.py -v`
Expected: FAIL (no access check yet — the "cannot" tests fail because no error is raised)

- [ ] **Step 3: Implement**

For each endpoint in the classification table, add the matching `ProjectAccessBLL.assert_can(call.identity, task.project, capability, object_owner=task.user)` call immediately after the point where the endpoint already loads the `Task` (or tasks, for a `*_many`/multi-id endpoint) and before any mutation. For the `*_many` endpoints, loop over every loaded task and call `assert_can` for each before performing any write on any of them. For `get_all`/`get_all_ex`/`get_by_id`/`get_by_id_ex`/`get_types`, use the same `project__in=ProjectAccessBLL.visible_project_ids(call.identity)` pattern as Task 5 (skip the filter entirely when `visible_project_ids` returns `None`). For `move`, call `assert_can(..., "admin")` on both the task's current project and the destination project. For `create`/`clone`/`validate`, call `assert_can(..., "write", object_owner=call.identity.user)` on the project the new/cloned task will belong to (for `create`, this is `data.project`; read the function to find the exact variable holding the resolved project id before the task is actually saved).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest apiserver/tests/services/test_tasks_access_enforcement.py -v`
Expected: PASS

- [ ] **Step 5: Run the full existing tasks test suite to confirm nothing else broke**

Run: `python -m pytest apiserver/tests -k task -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add apiserver/services/tasks.py apiserver/tests/services/test_tasks_access_enforcement.py
git commit -m "Enforce project access in tasks.py"
```

---

### Task 7: Enforce access in `models.py`

**Files:**
- Modify: `apiserver/services/models.py`
- Test: `apiserver/tests/services/test_models_access_enforcement.py` (new)

**Interfaces:**
- Consumes: `ProjectAccessBLL` from Task 2 (and `visible_project_ids` from Task 5).

Same shape as Task 6, applied to `Model` instead of `Task` (`Model.project`, `Model.user`). Do not touch `models.make_public`/`make_private`.

| Endpoint(s) | Capability | object_owner |
|---|---|---|
| `get_by_id`, `get_by_task_id`, `get_all_ex`, `get_by_id_ex`, `get_all`, `get_frameworks` | `read`, via `project__in=visible_project_ids(...)` | — |
| `create`, `update_for_task` | `write` | `call.identity.user` |
| `edit`, `update`, `set_ready`, `update_tags`, `add_or_update_metadata`, `delete_metadata` | `write` | `model.user` |
| `publish_many`, `archive_many`, `unarchive_many` | `write`, all-or-nothing over the batch | each model's `.user` |
| `delete`, `delete_many` | `delete`, all-or-nothing over the batch for `delete_many` | each model's `.user` |
| `move` | `admin` on current and destination project | — |

- [ ] **Step 1: Write the failing test**

```python
# apiserver/tests/services/test_models_access_enforcement.py
from datetime import datetime
import pytest
from apiserver.apierrors import errors
from apiserver.database.model.auth import Role
from apiserver.database.model.model import Model
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.service_repo.auth import Identity

COMPANY = "c1"


def make_root(visibility=ProjectVisibility.private, members=(), id_="root1", owner="owner1"):
    project = Project(
        id=id_, company=COMPANY, user=owner, name=f"Project-{id_}", basename=f"Project-{id_}",
        created=datetime.utcnow(), visibility=visibility,
        members=[ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by=owner) for u, r in members],
    )
    project.save()
    return project


def make_model(project_id, owner, id_="model1"):
    model = Model(id=id_, company=COMPANY, user=owner, project=project_id, name=f"Model-{id_}", created=datetime.utcnow())
    model.save()
    return model


def identity(user, role=Role.user):
    return Identity(user=user, company=COMPANY, role=role)


class FakeCall:
    def __init__(self, ident, **data):
        self.identity = ident
        self.data = data


def test_read_write_cannot_delete_someone_elses_model():
    make_root(members=[("u2", ProjectRole.read_write)])
    make_model("root1", "owner1")
    from apiserver.services.models import delete as delete_endpoint
    from apiserver.apimodels.models import DeleteModelRequest
    with pytest.raises(errors.forbidden.NoWritePermission):
        delete_endpoint(FakeCall(identity("u2")), COMPANY, DeleteModelRequest(model="model1", force=True))
    assert Model.objects(id="model1").first() is not None


def test_collaborator_can_edit_someone_elses_model():
    make_root(members=[("u2", ProjectRole.collaborator)])
    make_model("root1", "owner1")
    from apiserver.services.models import edit as edit_endpoint
    edit_endpoint(FakeCall(identity("u2"), model="model1", name="Renamed"))
    assert Model.objects(id="model1").first().name == "Renamed"
```

(Verify `DeleteModelRequest`'s and `edit`'s actual field names in `apiserver/apimodels/models.py` and `apiserver/services/models.py` before trusting this test as written — the design intent, not the exact kwargs, is the authority here.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest apiserver/tests/services/test_models_access_enforcement.py -v`
Expected: FAIL

- [ ] **Step 3: Implement**

Same pattern as Task 6, applied per the classification table above.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest apiserver/tests/services/test_models_access_enforcement.py -v`
Expected: PASS

- [ ] **Step 5: Run the whole unit test suite**

Run: `python -m pytest -q -m "not integration" apiserver/tests`
Expected: PASS, same count as before plus every test added in Tasks 1–7

- [ ] **Step 6: Commit**

```bash
git add apiserver/services/models.py apiserver/tests/services/test_models_access_enforcement.py
git commit -m "Enforce project access in models.py"
```

---

### Task 8: End-to-end coverage

**Files:**
- Modify: `tests/e2e/test_05_artifacts.py`
- Create: `tests/e2e/test_access_control.py`

**Interfaces:**
- Consumes: the live server from Tasks 1–7, through the existing e2e fixtures (`support/client.py`, `support/api.py`), the same way every other e2e test does.

- [ ] **Step 1: Remove the expected-failure marker**

In `tests/e2e/test_05_artifacts.py`, find `test_a_member_who_is_not_the_owner_cannot_delete_the_run` and remove whatever currently marks it as a known/expected failure (an `xfail`-style decorator or a docstring note — read the test first to see the exact mechanism in this suite before removing it), since this plan makes it pass for real.

- [ ] **Step 2: Run it to confirm it now passes**

Run: `tests/e2e/docker/run.sh -k test_a_member_who_is_not_the_owner_cannot_delete_the_run`
Expected: PASS

- [ ] **Step 3: Write new end-to-end scenarios in `tests/e2e/test_access_control.py`**

Following this suite's existing fixture style (module-level fixtures, `workspace.run_json`, a second member account the way `test_05_artifacts.py` already creates one), cover: a Read/Write member can push and delete their own task but not another member's; a Collaborator can delete another member's task but not delete the project; a plain Read member cannot push a task; a non-member gets the project's id treated as not-found on a private project (same response shape as a genuinely missing id — assert on the error code, not just "it failed"); a non-member can read a public project's tasks but cannot enqueue one; an admin can do all of the above on a private project without being a member.

- [ ] **Step 4: Run the new file**

Run: `tests/e2e/docker/run.sh -k test_access_control`
Expected: PASS

- [ ] **Step 5: Run the full suite once to confirm nothing else regressed**

Run: `tests/e2e/docker/run.sh`
Expected: PASS (every existing test, plus the new ones)

- [ ] **Step 6: Commit**

```bash
git add tests/e2e/test_05_artifacts.py tests/e2e/test_access_control.py
git commit -m "End-to-end coverage for project access control"
```
