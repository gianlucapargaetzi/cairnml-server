// 3) Task logging: scalars, console lines and artifacts reach the server.
#import "_chronos.typ": *


Events are batched and sent to the server. Large files do not pass through the API: the server returns a presigned object-storage URL and the SDK uploads straight to RustFS.

#block(breakable: false)[
= Task logging
== Creating the run and sending events

#seq-diagram(("SDK", "Server"), (
  call("SDK", "Server", "POST tasks.create (project, name)"),
  reply("Server", "SDK", "task id"),
  call("SDK", "Server", "POST tasks.started"),
  reply("Server", "SDK", "ok; status in progress"),
  process("SDK", "Queue scalars, plots and console lines in memory; send them in batches"),
  call("SDK", "Server", "POST events.add_batch (batch of events)"),
  process("Server", "Validate each event; compress large plots; bulk-index into Elasticsearch; update the task's last values in MongoDB"),
  reply("Server", "SDK", "added count and per-event errors"),
))
]

#block(breakable: false)[
== Artifact upload (presigned, direct to object storage)

#seq-diagram(("SDK", "Server", "RustFS"), (
  call("SDK", "Server", "POST datachunks.prepare_upload (task, name, size, sha256)"),
  process("Server", "Check the caller can write to the task; create a pending datachunk"),
  reply("Server", "SDK", "presigned PUT URL, or part URLs for multipart"),
  call("SDK", "RustFS", "PUT object (or each part) to the presigned URL"),
  reply("RustFS", "SDK", "200 with ETag"),
  alt((
    (guard: "multipart upload", steps: (
      call("SDK", "Server", "POST datachunks.commit (id, part ETags)"),
      process("Server", "Mark the datachunk committed; the artifact appears on the task"),
      reply("Server", "SDK", "id, state"),
    )),
    (guard: "single PUT", steps: (
      process("SDK", "Nothing more to send; the object is stored"),
    )),
  )),
))

Console lines are sent in the same `events.add_batch` call as scalars, as the `log` event type, and appear on the task's Console tab.
]
