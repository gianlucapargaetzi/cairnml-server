# Database management

The server keeps three stores:

- **MongoDB** with two databases: `backend-db` for tasks, models and projects, and `auth-db` for access records.
- **Elasticsearch** for event and metric indices.
- **Redis** for short-lived state (login state, single-use assertion ids, revoked sessions) and a lock for start-up.

Object bytes live in RustFS, never in a database.

## Start-up

Only one server instance runs start-up at a time, under a lock in Redis.

```mermaid
sequenceDiagram
    participant Server
    participant Redis
    participant Elasticsearch
    participant MongoDB

    Server->>Redis: acquire start-up lock (120 s timeout)
    Redis-->>Server: lock held
    Server->>Elasticsearch: check whether indices are empty
    Elasticsearch-->>Server: empty or not
    Server->>MongoDB: check whether databases are empty
    MongoDB-->>Server: empty or not
    alt fresh install, both empty
        Note over Server: create index templates and mappings, the default company and its default queue
    else existing data
        Note over Server: apply missing mappings, run each migration newer than the stored version, in order
        Server->>MongoDB: record the new version in the versions collection
        MongoDB-->>Server: ok
    end
    Server->>Redis: release start-up lock
    Redis-->>Server: released
```

## Record writes

```mermaid
sequenceDiagram
    participant Server
    participant MongoDB
    participant Elasticsearch
    participant Redis

    Note over Server: validate the call, run the business logic of the service
    Server->>MongoDB: read and update tasks, projects, queues, devices or users
    MongoDB-->>Server: ok
    alt event batch
        Server->>Elasticsearch: bulk-index events, one index per company and event type
        Elasticsearch-->>Server: per-event results
        Note over Server: update the task's last scalar values in MongoDB
    else replay-sensitive action
        Server->>Redis: set key with TTL (assertion id, login state, revoked session)
        Redis-->>Server: stored, or already present
    end
```

Writes are not in one transaction across the stores. A failure after one write leaves that write in place, and the client receives an error.

## Removing objects

Deleting a task removes its metadata at once. The stored objects are removed later by a background job, which retries failures.

```mermaid
sequenceDiagram
    participant Server
    participant MongoDB
    participant RustFS

    Server->>MongoDB: tasks.delete, remove the task and its events, insert pending delete records
    MongoDB-->>Server: ok
    Note over Server: background job picks pending records in batches
    Server->>RustFS: DELETE object (S3 API)
    RustFS-->>Server: 204, or an error
    alt object removed
        Server->>MongoDB: mark the delete record done
    else removal failed
        Server->>MongoDB: increment the retry count, mark failed after the maximum
    end
```

Records that have failed stay in the database, so they can be retried.

## Migrations

Each release that changes stored data ships a migration script in `apiserver/mongo/migrations/`. Scripts run at start-up, and only when the databases already contain data. A fresh install is created directly at the current version.

```mermaid
sequenceDiagram
    participant Server
    participant MongoDB

    Server->>MongoDB: read the versions collection, highest version applied
    MongoDB-->>Server: current version
    Note over Server: list migration scripts with a newer version, sorted
    Note over Server: run each script against backend-db and auth-db
    Server->>MongoDB: write a version record for each applied script
    MongoDB-->>Server: ok
```

Rolling back to an older server is not supported once a newer migration has run.
