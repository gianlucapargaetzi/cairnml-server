# Repository identifier names

The `repositories.*` actions name the repository they act on with one field, `ref`: its id, its name, or the address of its
repository. `repositories.resolve`, which takes several, names them `refs`. `user_access.*` names an access record, not a
repository, so it keeps `id`.

## Old names are refused

The names the server accepted before the SDK and the web app sent `ref` are gone: `id` and `repository` for the 14 actions
that name one repository, and `repositories` for `repositories.resolve`. A request that sends one of them fails with a
validation error that names the field. The SDK and the web app send `ref` and `refs` (cairnml#88 and cairnml-web#130).

## Unknown fields

A request class refuses a field it does not declare. The call fails with a validation error naming the field, instead of
the field being dropped without a word.
