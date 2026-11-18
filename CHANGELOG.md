## 0.0.6

The server's API level is 2.41. Each endpoint is served at the version its definition began, and its endpoints are generated into the SDK and the web app from its schema.

- The API change check blocks a breaking change unless the API version is raised and this changelog names the endpoint.
  It also checks changes inside shared definitions, and a field that changes to or from a union of shapes.
- The release is planned from the version files of the server, the SDK, the agent and the web app. A releasing component
  must have its section in its CHANGELOG.md. The image is built from the commits the release picked, and carries the
  "What's new" bundle of the release notes.
- Removed in 2.41: projects.get_all_ex's non_public and login.supported_modes' server_errors (older definitions still
  take or report them, see API 2.41). models.get_by_task_id and models.update_for_task are refused above API 2.13.
- queues.get_all_ex and projects.get_queues return the fields the web app reads (entries count, workers, the share level and
  id of a queue), and the schema declares them.

# Changelog

Each API version has a section. A change that breaks a client (a removed or changed endpoint, a field that became
required, a changed type) needs a raised API version and a line for the endpoint in that version's section; the
API change check in CI enforces this.

The schema keeps, per endpoint, one block for each change of that endpoint's definition, labelled with the API version
in which the definition began, and at most five blocks per endpoint. A request for version V is served by the newest
block whose version is at most V. An endpoint that did not change since 2.23 is valid at 2.23 and at every later version.

## API 2.41

The API level is 2.41. Every other endpoint keeps the version in which its definition began. Compared with the
previous release (API 2.39), these endpoints change in a way that breaks a client that still uses an older version:

- models.get_by_task_id: removed; it is refused above API 2.13 (use models.get_by_id or models.get_all)
- models.update_for_task: removed; it is refused above API 2.13 (use tasks.add_or_update_model)
- projects.get_all_ex: the request field non_public is removed; use allow_public. Versions 2.23 to 2.40 still take
  non_public, as the inverse of allow_public (allow_public wins when both are sent); 2.41 ignores it.
- login.supported_modes: the response field server_errors is removed. Versions 2.9 to 2.40 still report it (both flags
  are false on a running server); 2.41 does not.
- events.add_batch: the batched request is at API 2.41 (the request contract is not classified by the check). Versions
  2.22 to 2.40 keep the 2.22 definition, whose batched request is at 2.22; versions 2.1 to 2.21 keep the 2.1 definition.
- tasks.update_batch: the batched request is at API 2.41 (the request contract is not classified by the check). Versions
  2.1 to 2.40 keep the 2.1 definition, whose batched request is at 1.5.
- Definitions dropped to keep at most five per endpoint (the newest are kept): models.get_all_ex 2.1; projects.get_all_ex
  2.1, 2.13, 2.14, 2.15, 2.16, 2.17 and 2.20; queues.get_all_ex 2.4; tasks.get_all_ex 2.1 and 2.13. A request at a dropped
  version is refused, naming the oldest version that is still served.
- A request below the oldest retained definition of an endpoint is refused (404, subcode 1), naming the oldest version
  that is still served. Any other request is served by the newest definition at most its version.
