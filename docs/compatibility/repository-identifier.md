# Repository identifier names

The `repositories.*` actions name the repository they act on with one field, `ref`: its id, its name, or the address of its
repository. `repositories.resolve`, which takes several, names them `refs`. `user_access.*` names an access record, not a
repository, so it keeps `id`.

## Deprecated names

Until the SDK and the web app send `ref`, the server still accepts the names they sent before. Each use is logged as a
warning with the action and the name (`repositories.delete: 'id' is deprecated, send 'ref'`).

| Action | Deprecated names | Replacement |
|---|---|---|
| `delete`, `set_shared`, `check_sharing`, `link`, `unlink`, `assign`, `unassign`, `check`, `refresh`, `get_runs`, `get_history`, `get_dataset_versions`, `get_details`, `git_token` | `id`, `repository` | `ref` |
| `resolve` | `repositories` | `refs` |

When several are sent, `ref` wins over `id`, and `id` over `repository`. `get_details` and `git_token` never took `id`
before; it is accepted with the others only so that all 14 actions share one rule.

The list is `DEPRECATED_ALIASES` in `apiserver/tests/service_repo/test_repository_identifier_names.py`; a test checks that it
equals the set of actions whose request class has an alias field.

## Unknown fields

A request class refuses a field it does not declare. The call fails with a validation error naming the field, instead of
the field being dropped without a word.

## Removal

The old names are kept for at least one release. They are removed, together with `DEPRECATED_ALIASES`, in a later change that
comes after the SDK release and the web app build that send `ref` and `refs`.
