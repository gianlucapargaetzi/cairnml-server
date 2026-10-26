# Per-project access control: design

Status: approved; ready for a plan.

Closes cairnml-stack#4 ("Access Management and Groups for collaborative work") and cairnml-server#2 ("Plain users can delete another user's run").

## Intent

Today, inside one company, any signed-in user can read, edit or delete any project, task or model. There is a company-wide `admin` role (synced from the `cairnml-admin` Keycloak realm role), and nothing finer than that. There is no production data to migrate — the owner has never run this stack in production, so this lands on a clean slate.

The goal is per-project membership and roles, with two visibility levels, so a project's owner controls who can see it and what they can do to it, without needing an admin for every change.

## Goals

1. Every project has exactly one **Owner** — whoever creates it (explicitly, or by being the first to push a task into a project name that doesn't exist yet).
2. The Owner can add other users to the project with one of three roles: **Collaborator**, **Read/Write**, **Read**. Roles can be changed and revoked later by anyone who could have granted them.
3. A project is **public** (every signed-in user can read it; a non-member has an implicit Read) or **private** (invisible and inaccessible to anyone not on its member list). New projects default to private.
4. A private project is **fully hidden** from non-members: it does not appear in any listing or search, and a direct request for its id gets the same not-found response as a genuinely nonexistent id. Consequently, project names are unique only among the projects a given user can see, not company-wide as today — and when a push names a project that collides with one that exists but is invisible to the pusher (someone else's private project of the same name), it always creates a new project owned by the pusher. It never attaches to, reveals, or merges with the existing one; anything else would be a access-control bug, not a design choice.
5. Sub-projects (`Robotics/Perception` under `Robotics`) inherit their membership and visibility from the top-most project in their path. There is no such thing as owning or separately configuring only a sub-project.
6. Company `admin` (existing role) bypasses every one of these checks, on every project, always.
7. The web app gets a **Members** tab in the existing Project Settings dialog, and visibility + an optional initial member list are part of the project-creation form. Both are built from the app's existing components and conventions (see "UI" below) — a reviewed mockup for the Members tab already exists.
8. A full role × action test matrix, in both `cairnml-server`'s unit tests and the end-to-end suite.

## Non-goals (discovered during implementation, documented here retroactively)

- **`reports.py`'s own endpoints** (`reports.move`, `reports.create`, and any others) get no `ProjectAccessBLL.assert_can` checks of their own in this plan. Only `projects.py`, `tasks.py` and `models.py` got a full enforcement sweep; `reports.py` was touched only incidentally, to thread `identity` through the shared project-lookup fix (closing the hidden-collision fork). This was an implicit scoping choice made while writing the implementation plan, never written down until a later task's own investigation surfaced it — a real gap in how this round was planned, not a decision anyone reviewed. A report can still be moved into or created under a project its caller has no business touching, via `reports.move`/`reports.create`, exactly as before this plan. Closing it needs its own classification table and task, the same shape as the ones this plan already did for projects/tasks/models.
- Closed before merge: `reports.create` now requires write on the named project and `reports.move` requires admin on both the report's current project and its destination, mirroring `tasks.move`. The shared `ProjectBLL.find_or_create` also requires write access to whatever project it resolves or creates, for every caller.

## Non-goals

- Migrating existing data. There is none to migrate; every project created from now on gets the rules below from the moment it's created.
- Groups of users (inviting a whole team at once). Members are added one at a time, by user.
- Changing company-wide roles (`admin`, `user`, ...) or how they're synced from Keycloak. `admin` keeps meaning exactly what it means today.
- Per-project control over queues/workers. A queue stays company-wide infrastructure; see "Queues and jobs" below for how enqueueing is actually gated.
- A "longest-standing Collaborator" or any other automatic fallback when a project becomes ownerless. It stays ownerless until an admin assigns a new Owner.
- Letting a Collaborator remove, demote or replace the Owner, or force an ownership transfer.
- Fixing the pre-existing gap where a task with no project is visible company-wide. Unchanged by this work.

## Anti-goals

These would make the design a failure even if it technically runs:

- A non-member of a private project learning that it exists at all — through a listing, a search result, a name-collision error, or a different HTTP status than a truly-missing id would get.
- A Collaborator managing to delete the project, or to strip the Owner's role, through some endpoint this design didn't enumerate.
- A new UI surface that a user can immediately tell "doesn't belong" next to the rest of the app (wrong component, wrong spacing, a new visual language). The Members tab mockup below was reviewed and approved specifically against this risk.
- Baking a per-project role into a long-lived token (a device/job token). Roles are read fresh from the database on every request; a role revoked five seconds ago must take effect on the very next call, including calls made by an agent running a job on the revoked user's behalf.

## Current state (read, not changed by this design)

- `apiserver/database/model/auth.py`: `Role` (`system`, `root`, `admin`, `superuser`, `user`, `annotator`, `guest`) is a company-wide role on `User`, unrelated to any project. `apiserver/idp/users.py` maps the `cairnml-admin` Keycloak realm role to `Role.admin`; everything else becomes `Role.user`. This is untouched.
- `apiserver/database/model/__init__.py`: `AttributedDocument` (base of `Project`, `Task`, `Model`, ...) already has `company` and `user` (creator) fields. `Task.project` and `Model.project` already reference `Project`.
- `apiserver/database/model/project.py`: `Project` already has `parent` (a reference to another `Project`) and `path` (the list of ancestor ids), so nested projects already exist; `ProjectBLL.create` already auto-creates missing ancestors along a `/`-separated name and already records the creating user.
- `apiserver/database/model/queue.py`: `Queue` has no `project` field. It is, and stays, company-wide.
- Today, `projects.create` has no admin-only restriction — any signed-in user can already create a project, via the API and (as far as this exploration found) the web app too. The older issue's claim that "only admin is allowed to create projects" no longer matches the code; no work is needed to open that up, it's already open.
- `cairnml-web`'s `isReadOnly` helper (`webapp-common/shared/utils/is-read-only.ts`) is an unrelated, older concept (a company-less "shared example" marker inherited from upstream ClearML). The new visibility field gets its own name so the two are never confused.
- `cairnml-server`'s own identity (`service_repo/auth/identity.py`) carries `user`, `company`, `role` (the company-wide one) — no project-scoped claim. This stays true; see the anti-goal above.

## Decisions made in this conversation

- Scope for this round: **both** `cairnml-server` (data model, enforcement, API) and `cairnml-web` (Members tab, creation-form additions). Not phased.
- New projects default to **private**.
- A private project is **fully hidden** (404, not 403, for a non-member); project-name uniqueness is scoped to "visible to the requester," not company-wide.
- Sub-projects **inherit** membership and visibility from the top-most ancestor; there is no independent per-sub-project configuration.
- Ownership transfer: only the current Owner, or an admin, can change who the Owner is. A Collaborator cannot.
- An ownerless project (the Owner's account removed from Keycloak) stays ownerless until an admin assigns one. No automatic fallback.
- No data migration: there is nothing to migrate.
- The Members tab goes inside the existing Project Settings dialog (`mat-tab-group`, "with-border" styling), as a new tab after the existing ones. Mockup reviewed and approved in this conversation (`.superpowers/brainstorm/.../members-tab-mockup.html`, not committed — a throwaway visual aid, not a spec).

## The role matrix

Every action below is checked against the project a task/model belongs to (resolved to its top-most ancestor, per "Sub-projects" above). "Own" means `object.user == caller`.

| Action | Owner | Collaborator | Read/Write | Read (explicit or implicit on a public project) |
|---|---|---|---|---|
| Read the project, its tasks, models | ✓ | ✓ | ✓ | ✓ |
| Create/push a task or model into the project | ✓ | ✓ | ✓ | ✗ |
| Edit a task/model, anyone's | ✓ | ✓ | own only | ✗ |
| Delete a task/model, anyone's | ✓ | own only | own only | ✗ |
| Enqueue/dequeue/abort a task | ✓ | ✓ | own only | ✗ |
| Edit project settings (rename, move, default output destination) | ✓ | ✓ | ✗ | ✗ |
| Add/remove a member, change a member's role (not the Owner's) | ✓ | ✓ | ✗ | ✗ |
| Change visibility (public/private) | ✓ | ✓ | ✗ | ✗ |
| Transfer ownership | ✓ | ✗ | ✗ | ✗ |
| Delete the project | ✓ | ✗ | ✗ | ✗ |

Admin: every cell is ✓, on every project, with no membership entry needed.

Two asymmetries worth calling out explicitly, because they follow from the owner's own wording rather than being obvious:
- **Collaborator can edit anyone's task but can only delete their own.** The owner's instructions named "remove a task from someone else" as the one thing Collaborator can't do to another member's object; editing wasn't listed, so it's allowed.
- **Read/Write is restricted to "own" for every write action, including enqueue.** "Schedule jobs on workers" is listed as an Owner/Collaborator ability; Read/Write only gets to act on objects it created, so it may enqueue its own tasks but not anyone else's.

## Data model

On `Project` (used only when the project is a root — i.e. `parent` is unset):
- `visibility`: `public` | `private`, default `private`.
- `members`: a list of `{user, role, added, added_by}`, where `role` is one of `collaborator | read_write | read`. The Owner is **not** stored as a member row; `Project.user` (the existing creator field) already is, and always will be, the Owner. (An ownerless project simply has no valid Owner — `user` can be cleared by an admin action, see below.)

A non-root project (one with `parent` set) does not use these fields; every check resolves to the root first.

New BLL surface (shape, not final code — the plan details it):
- `ProjectAccessBLL.resolve_root(project_id) -> project_id`: walks `parent` to the top. Called often enough (every request touching a project-scoped object) that the plan should consider denormalizing a `root` field onto every `Project` at creation/move time, rather than walking on every call.
- `ProjectAccessBLL.get_role(identity, project_id) -> Role | None`: `admin` → treated as Owner-equivalent everywhere without a membership row; otherwise resolves the root and looks up `user == root.user` (Owner), else the member list, else `read` if `root.visibility == public`, else `None` (no access at all).
- `ProjectAccessBLL.assert_access(identity, project_id, action, object_owner=None)`: raises the existing `errors.forbidden` (403) for a known-but-disallowed action on a project the caller can see, or the existing **not-found** error (`bad_request.InvalidProjectId`/`ProjectNotFound`, today a 400 — this codebase has no 404 category and inventing one company-wide is out of scope) when the caller has no role at all on a private project. The point isn't the status number, it's that the response must be byte-for-byte what a genuinely nonexistent id already gets today — reusing the exact existing error achieves that for free; a new status code would not.
- `ProjectAccessBLL.visible_projects_filter(identity) -> query`: used by `projects.get_all`, `tasks.get_all`, etc. to exclude roots the caller can't see, instead of checking after the fact.

## API surface

- `projects.create` gains optional `visibility` and `members` (list of `{user, role}`), so the web creation form can set them in the same call that creates the project (see "UI").
- New endpoints (final names left to the plan): `projects.get_members`, `projects.add_member`, `projects.update_member_role`, `projects.remove_member`, `projects.set_visibility`, `projects.transfer_ownership`.
- `transfer_ownership` requires its target to already be a member of the project (any role) — transfer can never be the route by which someone gains access to a private project they couldn't see a moment before. On success, the new Owner's member row is removed (the Owner isn't stored as a member row, per the data model above) and the outgoing Owner is added back as a `collaborator` member, so giving the project away never costs the previous Owner their own access to it.
- Every existing endpoint that reads or writes a project-scoped object (`tasks.*`, `models.*`, `projects.*`, and anything that touches artifacts/configuration/logs of a task) needs the matching `assert_access` or `visible_projects_filter` call added. This is the largest single piece of implementation work and deserves a full inventory in the plan, not a guess here — it spans most of `apiserver/services/`.
- Job/agent execution: a running job authenticates as a device/job token that already resolves back to the task's owning user (confirmed in `apiserver/services/devices.py`); writes it makes during the run (status, logs, artifacts) are therefore naturally checked as "edit own task" and need no new mechanism.

## UI

- **Project Settings dialog** (`shared/project-dialog/project-settings/project-settings-dialog.component.ts`): a new `MEMBERS` tab, built from the same chrome as `GENERAL`/`SCALAR VIEW DEFAULTS` (same `mat-tab-group`, same dialog actions row). Content: a visibility toggle (`sm-button-toggle`, "Public"/"Private"), a member list (`sm-table`-style rows: avatar, name, email, role, remove), "Add member" as the one primary action for the section (top right, per the button rules), and `sm-confirm-dialog` before a remove. The Owner's row has no role control and no remove control. Reviewed mockup: approved in this conversation.
- A **sub-project's** Members tab shows the inherited list read-only, with a link up to the root project where it's actually managed (consistent with "Sub-projects" above).
- **Project creation**: the existing create-project form gains the same visibility toggle and an optional initial member list, reusing the Members tab's row component rather than inventing a second one. (This responds to the older issue's request for a single creation "setup mask" that sets name, description, scope and collaborators together; it doesn't need its own mockup round — it's the already-approved member-row widget embedded in an existing form instead of the settings dialog.)
- Buttons, actions and destructive confirmations elsewhere in the app that currently assume "anyone can do this" (e.g. a task's delete action, a project's delete action) get hidden or disabled according to the matrix above, using whatever role-check the API now exposes to the client (the plan should settle whether that's a field on the already-fetched project/task, or a dedicated lightweight endpoint).

## Testing

- `cairnml-server` unit tests: the full role × action matrix above, for both a root project and a sub-project (confirming inheritance), for both public and private visibility, including the two asymmetries called out above, the ownerless-project state, and the 404-not-403 behavior for a private project.
- End-to-end: extends the existing fixtures (`test_05_artifacts.py`) rather than inventing new scaffolding. Removes the `integration`/expected-failure marker on `test_a_member_who_is_not_the_owner_cannot_delete_the_run` once it passes for real; adds the public/private and sub-project cases there or in a new `test_access_control.py`.
- `cairnml-web`: unit tests for the new Members tab and the creation-form addition in whatever harness the app already uses (Karma); no new web test infrastructure.

## Left to the builder (plan-level decisions, not blocking approval)

- Final endpoint names and request/response shapes for the new `projects.*` member/visibility/ownership endpoints.
- Whether `root` is denormalized onto `Project` or resolved by walking `parent` on every request, and how a project **move** (there's already a "move to" action in the project card menu) updates that denormalized value and is itself access-checked (moving a project changes whose rules apply to it; the plan should require Owner/admin on both the source and destination trees). One piece settled here rather than left open: `visibility`/`members` are cleared, not kept dormant, the moment a root project is demoted into a sub-project (a stale member list silently reappearing if it's later promoted back to root would be its own bug); a project promoted to root gets fresh defaults (private, no members), never a resurrected prior state.
- The exact inventory of existing `apiserver/services/*` endpoints that need an `assert_access`/`visible_projects_filter` call added.
- How the web app learns a user's role on the currently-open project without an extra round-trip per screen (a field on the project/task payload vs. a dedicated call).
- Error payload details for the new 404-for-hidden-private-project behavior (it must look identical to a genuinely missing id to the caller).
