# Task logging

A running script sends its metrics, plots and console output to the server in batches. Large files do not pass through the API. The server returns a presigned URL, and the SDK uploads the file straight to object storage (RustFS).

## Events

```mermaid
sequenceDiagram
    participant SDK
    participant Server

    SDK->>Server: POST tasks.create (project, name)
    Server-->>SDK: task id
    SDK->>Server: POST tasks.started
    Server-->>SDK: ok, status in progress
    Note over SDK: queue scalars, plots and console lines, send them in batches
    SDK->>Server: POST events.add_batch (batch of events)
    Note over Server: validate each event, compress large plots
    Note over Server: bulk-index into Elasticsearch, update the task's last values in MongoDB
    Server-->>SDK: added count and per-event errors
```

## Artifacts

```mermaid
sequenceDiagram
    participant SDK
    participant Server
    participant RustFS

    SDK->>Server: POST datachunks.prepare_upload (task, name, size, sha256)
    Note over Server: check write access to the task, create a pending datachunk
    Server-->>SDK: presigned PUT URL, or part URLs for multipart
    SDK->>RustFS: PUT object (or each part) to the presigned URL
    RustFS-->>SDK: 200 with ETag
    alt multipart upload
        SDK->>Server: POST datachunks.commit (id, part ETags)
        Note over Server: mark the datachunk committed, the artifact appears on the task
        Server-->>SDK: id, state
    else single PUT
        Note over SDK: nothing more to send, the object is stored
    end
```

## Notes

- The server records the sha256 that the SDK declared when it prepared the upload. It never holds the file bytes.
- Console output uses the same `events.add_batch` call, as the `log` event type, and appears on the task's Console tab.
- Presigned URLs are valid for 15 minutes.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `events.add_batch` | Stores a batch of metrics, plots and console lines |
| `datachunks.prepare_upload` | Creates the datachunk and returns the presigned URL(s) |
| `datachunks.commit` | Completes a multipart upload |
