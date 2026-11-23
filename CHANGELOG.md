## 0.0.6

The server's API level is 2.41. The SDK and the web app are generated from the server's schema, so they use the same level.

- Removed in API 2.41: the request field non_public of projects.get_all_ex (send allow_public instead) and the response field server_errors of login.supported_modes. A client that asks for API 2.40 or older still gets the old behaviour; only clients at 2.41 do not.
- models.get_by_task_id and models.update_for_task are refused above API 2.13. Use models.get_by_id or models.get_all, and tasks.add_or_update_model.
- The queue list (queues.get_all_ex) returns the number of entries and the workers of each queue when the request asks for them, and the queues of a project (projects.get_queues) return the share level and id of each queue. The schema declares these fields, so the SDK and the web app can read them.
- Every change that affects an API client is listed in the section "API 2.41" of the server's CHANGELOG.md.
- The agent is now part of the SDK: install it with pip install "cairnml[agent]". The server image no longer uses a separate agent package. The agent's command, its configuration files and its environment names stay the same.

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
- tasks.get_by_id, tasks.get_all, tasks.get_by_id_ex, tasks.get_all_ex, reports.get_task_data, tasks.create, tasks.edit,
  tasks.validate, tasks.clone: the shared definitions of a task declare its project, its parent and the model of each
  input and output model as the ID or as the expanded object (`project` and the model: `{id, name, ...}`, `parent`:
  `{id, name, project}`), where they declared the ID only. This corrects the declaration and changes nothing on the
  wire, for any API version. Responses: the previous release already returned the object from tasks.get_by_id_ex,
  tasks.get_all_ex and reports.get_task_data (joined references are expanded, even without a projection) and the ID from
  tasks.get_by_id and tasks.get_all, and they still do. Requests: tasks.create, tasks.edit, tasks.validate and
  tasks.clone take the model ID only, as before, and refuse an object; the declaration of their request is wider than
  what they accept.
- projects.get_queues: the response declares the level and share_id of a queue, which the server already returned from
  2.38 on. The contract is the same at every version; the definition labelled 2.41 exists so that the change is recorded
  at 2.41, and the one labelled 2.38 still serves 2.38 to 2.40.
- Definitions dropped to keep at most five per endpoint (the newest are kept): models.get_all_ex 2.1; projects.get_all_ex
  2.1, 2.13, 2.14, 2.15, 2.16, 2.17 and 2.20; queues.get_all_ex 2.4; tasks.get_all_ex 2.1 and 2.13. A request at a dropped
  version is refused, naming the oldest version that is still served.
- A request below the oldest retained definition of an endpoint is refused (404, subcode 1), naming the oldest version
  that is still served. Any other request is served by the newest definition at most its version.
- devices.git_token: the response declares url, the clone address of the repository the token is for, which the server
  already returns. The contract is the same at every version; the definition labelled 2.41 exists so that the change is
  recorded at 2.41, and the one labelled 2.8 still serves 2.8 to 2.40.
- tasks.update: the request declares status_message, which the handler already reads. The contract is the same at every
  version; the definition labelled 2.41 exists so that the change is recorded at 2.41, and the one labelled 2.1 still
  serves 2.1 to 2.40.
- tasks.enqueue: the request declares force, which the handler already reads. The contract is the same at every version;
  the definition labelled 2.41 exists so that the change is recorded at 2.41, and the one labelled 2.31 still serves
  2.31 to 2.40.
