# Compatibility matrix

Status values: `compatible` (works as upstream), `changed` (works, under a different name, command or path, see the
migration column), `removed` (not in CairnML), `not planned` (removed on purpose, no replacement is planned).

The test `apiserver/tests/test_compatibility_matrix.py` reads this table. Every area listed below must have a row, every
status must be one of the four values, every row must have a migration path, and each link must point at a file that
exists.

| Feature area | Upstream (ClearML) | CairnML | Status | Migration |
|---|---|---|---|---|
| CLI: enrolment | `clearml-init` | `cairnml init <server>` (signs in with this machine's device key) | changed | [CLI migration](cli-migration.md#enrolment) |
| CLI: tasks | `clearml-task` | `cairnml task`, with the same options for project, name, repository, queue and requirements | changed | [CLI migration](cli-migration.md#tasks) |
| CLI: datasets | `clearml-data` | `cairnml dataset` (Git repository, files in DVC or Git LFS) | changed | [Dataset migration](dataset-migration.md) |
| CLI: parameter search | `clearml-param-search` | `cairnml param-search` | changed | [CLI migration](cli-migration.md#parameter-search) |
| CLI: debugging | `clearml-debug` | `cairnml debug` | changed | [CLI migration](cli-migration.md#debugging) |
| CLI: agent | `clearml-agent` | `cairnml-agent`, a separate package and command | changed | [CLI migration](cli-migration.md#agent) |
| Python import | `from clearml import Task` | `from cairnml import Task` | changed | [CLI migration](cli-migration.md#python-import) |
| Dataset workflow | ID-based datasets, `clearml-data` create, add, close, get | repository-based datasets: `cairnml dataset create`, init, add, commit, push, register, get | changed | [Dataset migration](dataset-migration.md) |
| API access keys | `CLEARML_API_ACCESS_KEY` and `CLEARML_API_SECRET_KEY` in the environment or the config file | removed; each machine signs in with its own device key | removed | [Auth and storage](auth-and-storage.md#sign-in-and-access-keys) |
| Web sign-in and accounts | local accounts, self-registration, a credential page in the web app | Keycloak sign-in only; an administrator creates every account | changed | [Auth and storage](auth-and-storage.md#web-sign-in-and-accounts) |
| Server auth modes | fixed and basic sign-in modes | removed; Keycloak is the only sign-in | removed | [Auth and storage](auth-and-storage.md#server-sign-in-modes) |
| File storage | the server serves files | object storage (S3-compatible) through presigned URLs, datasets through DVC or Git LFS | changed | [Auth and storage](auth-and-storage.md#file-storage) |
| Local configuration file | `~/clearml.conf` | `~/cairnml.conf`; `~/trains.conf` is still read | changed | [CLI migration](cli-migration.md#configuration-file) |
| Agent app metering and proxy | app metering, proxy and wrapper for desktop and CLI app traffic | not in the agent; no setting turns it on | not planned | [Agent metering](agent-metering.md) |
