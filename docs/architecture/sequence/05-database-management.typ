// 5) Database management: start-up, record writes, object removal, migrations.
#import "_chronos.typ": *


The server keeps MongoDB (two databases: `backend-db` for tasks, models and projects, and `auth-db` for access records), Elasticsearch (event and metric indices) and Redis (short-lived state and a start-up lock). Object bytes live in RustFS, never in a database.

#block(breakable: false)[
= Database management
== Start-up: initialise or migrate

#seq-diagram(("Server", "Redis", "Elasticsearch", "MongoDB"), (
  call("Server", "Redis", "acquire start-up lock (120 s timeout)"),
  reply("Redis", "Server", "lock held"),
  call("Server", "Elasticsearch", "check whether indices are empty"),
  reply("Elasticsearch", "Server", "empty or not"),
  call("Server", "MongoDB", "check whether databases are empty"),
  reply("MongoDB", "Server", "empty or not"),
  alt((
    (guard: "fresh install (both empty)", steps: (
      process("Server", "Create index templates and mappings; create the default company and its default queue"),
    )),
    (guard: "existing data", steps: (
      process("Server", "Apply missing index mappings; run each migration script newer than the stored version, in order"),
      call("Server", "MongoDB", "record the new version in the versions collection"),
      reply("MongoDB", "Server", "ok"),
    )),
  )),
  call("Server", "Redis", "release start-up lock"),
  reply("Redis", "Server", "released"),
))
]

#block(breakable: false)[
== Record writes

#seq-diagram(("Server", "MongoDB", "Elasticsearch", "Redis"), (
  process("Server", "Validate the call; run the business logic of the service"),
  call("Server", "MongoDB", "read and update task, project, queue, device or user documents"),
  reply("MongoDB", "Server", "ok"),
  alt((
    (guard: "event batch", steps: (
      call("Server", "Elasticsearch", "bulk-index events (one index per company and event type)"),
      reply("Elasticsearch", "Server", "per-event results"),
      process("Server", "Update the task's last scalar values in MongoDB"),
    )),
    (guard: "replay-sensitive action", steps: (
      call("Server", "Redis", "set key with TTL (assertion id, login state, revoked session)"),
      reply("Redis", "Server", "stored, or already present"),
    )),
  )),
))
]

#block(breakable: false)[
== Removing objects (asynchronous delete)

#seq-diagram(("Server", "MongoDB", "RustFS"), (
  call("Server", "MongoDB", "tasks.delete: remove the task and its events; insert pending delete records"),
  reply("MongoDB", "Server", "ok"),
  process("Server", "A background job picks pending delete records in batches"),
  call("Server", "RustFS", "DELETE object (S3 API)"),
  reply("RustFS", "Server", "204 or error"),
  alt((
    (guard: "object removed", steps: (
      call("Server", "MongoDB", "mark the delete record done"),
      reply("MongoDB", "Server", "ok"),
    )),
    (guard: "removal failed", steps: (
      call("Server", "MongoDB", "increment retry count; mark failed after the maximum"),
      reply("MongoDB", "Server", "ok"),
    )),
  )),
))
]

#block(breakable: false)[
== Schema migrations

Each release that changes stored data ships a migration script in `apiserver/mongo/migrations/`. Scripts run at start-up, only when the databases already contain data.

#seq-diagram(("Server", "MongoDB"), (
  call("Server", "MongoDB", "read the versions collection: highest version applied"),
  reply("MongoDB", "Server", "current version"),
  process("Server", "List migration scripts with a newer version, sorted; run each against backend-db and auth-db"),
  call("Server", "MongoDB", "write a version record for each applied script"),
  reply("MongoDB", "Server", "ok"),
))
]
