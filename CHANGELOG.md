# Changelog

Each API version has a section. A change that breaks a client (a removed or changed endpoint, a field that became
required, a changed type) needs a raised API version and a line for the endpoint in that version's section; the
API change check in CI enforces this.

## API 2.41

One API version is served: every endpoint is at 2.41. Compared with the previous release (API 2.39), these
endpoints change in a way that breaks a client that still uses an older version:

- models.get_by_task_id: removed; it is refused at 2.41 (use models.get_by_id or models.get_all)
- models.update_for_task: removed; it is refused at 2.41 (use tasks.add_or_update_model)
- projects.get_all_ex: the request field non_public is removed; use allow_public
- login.supported_modes: the response field server_errors is removed
- events.add_batch: the batched request is at API 2.41 (the request contract is not classified by the check)
- tasks.update_batch: the batched request is at API 2.41 (the request contract is not classified by the check)
- Requests for any API version other than 2.41 are not served: older clients must move to 2.41.
