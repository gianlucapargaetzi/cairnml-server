# Storage gateway: concept

Written for: the engineers who maintain the MLOps Flow fork of ClearML (server, SDK, agent).

Status: proposal, branch `port/rustfs`. Nothing here is implemented yet.

## 1. Problem

Today the SDK sends binary data (artifacts, models, dataset files, debug images) to whichever
storage its configuration names. The default is the file server, a Flask app that writes to one
disk folder. We want:

- one global endpoint that the SDK calls for all binary data,
- storage backends registered with that endpoint: an S3-compatible store for datasets, the NAS
  over SFTP for models, local disk for artifacts, and later Azure or Google Cloud,
- the backend chosen by the kind of data, without the client naming a backend,
- bytes that do not pass through the gateway, to keep throughput up,
- our identity check (Keycloak tokens, device keys) on every step, as the rest of the server has.

Metrics, parameters and task metadata are out of scope. They stay in the API server, MongoDB and
Elasticsearch. Only the payloads move.

## 2. Current state (verified in the code)

### 2.1 What the SDK sends where

The routing is static. Each API call in the SDK always takes the same path. The only dynamic
part is the destination, and that is one setting per task.

| What | SDK entry point | Where the record goes | Where the bytes go |
|---|---|---|---|
| Scalars | `Logger.report_scalar` (`clearml/logger.py`), `ScalarEvent` (`clearml/backend_interface/metrics/events.py:161`) | API server events service, then Elasticsearch | none |
| Plots | `PlotEvent` (`events.py:202`) | same as scalars | none (the plot is JSON) |
| Debug images, media | `ImageEvent(UploadEvent)` (`events.py:455`), `report_image`, `report_media` | the event row, which holds the URL | the default upload destination (`Logger.get_default_upload_destination`, `logger.py:1218`) |
| Artifacts | `Task.upload_artifact` (`clearml/binding/artifacts.py:498`), `_upload_local_file` (`artifacts.py:1119`) | the task's artifact list in MongoDB | `task.output_uri`, else the logger default |
| Models | `OutputModel` (`clearml/model.py`, `upload_storage_uri`) | the model record | `task.output_uri` |
| Datasets | `Dataset` (`clearml/datasets/dataset.py`), a task with the system tag `dataset` (line 144) | the task, and the dataset's file list | `output_uri` (line 793) |

Where the bytes go is decided by one function: `StorageHelper.get(uri)` in
`clearml/storage/helper.py` picks a driver from the URI scheme:

- `file://` and plain paths: local disk,
- `http(s)://` the files-server host: `_HttpDriver`, a multipart `POST` with the user's token,
- `s3://`: `_Boto3Driver`, direct to S3 with keys from the client config,
- `gs://`, `azure://`: the Google and Azure clients, direct,
- nothing set: the default in `Session.get_files_server_host()` (`clearml/backend_api/session/session.py:755`), which is the file server.

So the mapping of data kind to API call is in the code, and the storage target is one URI per
task, set in `clearml.conf` (`development.default_output_uri`) or on the task. The server does
not decide where anything goes. It stores what the SDK reports.

### 2.2 The file server

`fileserver/fileserver.py` (clearml-server):

- `POST /` multipart upload into `UPLOAD_FOLDER` (default `/mnt/fileserver`),
- `GET /<path>` download (`send_from_directory`, 300 s cache),
- `DELETE /<path>` and `POST /delete_many` (batch, enabled by `fileserver.delete.allow_batch`),
- `GET /` ping.

Auth is off by default (`fileserver.auth.enabled=false`). When on, `fileserver/auth.py` asks the
API server `users.get_current_user` and caches the answer for 60 s.

Deletes run in the API server: `apiserver/jobs/async_urls_delete.py` has a `Storage` base class
with `FileserverStorage`, `AWSStorage`, `AzureStorage` and `GoogleCloudStorage`, selected by
`StorageType` (`database/model/url_to_delete.py`). This is already an adapter pattern, but only
for deletes.

The web app reads files through nginx at `/files` (`docker/build/internal_files/clearml.conf.template`).

## 3. Target design

### 3.1 Components

- **Storage gateway.** Our file server, extended. It owns the registry, the router, the
  signing, and the adapters. It is the only component that holds storage credentials.
- **Registry.** A config file. One entry per backend: `name`, `adapter` (`local`, `s3`,
  `sftp`, `azure`, `gcs`), `direct` (true when the adapter can sign URLs), the kinds it serves,
  and settings with secrets as files, never inline.
- **Router.** Maps `(kind, task)` to a backend name. The kind is `artifact`, `dataset`,
  `model` or `debug`.
- **Adapters.** One class per store, behind the interface in 3.4.

The SDK keeps one new driver, `GatewayDriver`. The old `_HttpDriver` stays for the migration.

### 3.2 The flow for an upload

```
SDK                       Gateway                    API server          Store
 |  prepare-upload         |                          |                   |
 |  {task, kind, name,     |  check token             |                   |
 |   size, sha256}   ----> |  task readable? ------>  |                   |
 |                         |  <-- task tags, project  |                   |
 |                         |  route(kind, task)       |                   |
 |                         |  key = <project>/<task>/<kind>/<name>        |
 |  <-- {location,         |  sign PUT (if direct)  ----------------->    |
 |        method, url,     |                                              |
 |        headers, expires}|                                              |
 |  PUT bytes to url ------------------------------------------------->    |
 |  commit {location,      |  HEAD object, compare size and sha256  ---->  |
 |          size, sha256}  |                                              |
 |  <-- ok                 |  record audit event                          |
 |  add artifact/event with location (through the API server as today)    |
```

For a proxied backend (local disk, SFTP), `url` is `PUT /storage/v1/data/<location>` on the
gateway itself. The gateway streams the request body to the adapter. Memory use stays flat.

### 3.3 The API

All paths are under the gateway's public host. Every call carries the user's IdP-backed token.

| Call | Body | Returns |
|---|---|---|
| `POST /storage/v1/prepare-upload` | `task`, `kind`, `name`, `size`, `sha256` (optional), `content_type` | `location`, `method`, `url`, `headers`, `expires_at`, `multipart` (phase 5) |
| `POST /storage/v1/commit` | `location`, `size`, `sha256` | `ok` or an error (missing object, size mismatch) |
| `POST /storage/v1/prepare-download` | `location` | `url`, `expires_at` (or a stream URL for proxied backends) |
| `GET /storage/v1/files/<location>` | none | `302` to a signed URL, or the bytes for proxied backends. This keeps the web app working unchanged. |
| `POST /storage/v1/delete` | `locations` | per-location result. The API server's async delete job calls this. |
| `GET /storage/v1/backends` | none (admin) | the registry, without secrets |

`location` has the form `<backend>://<key>`. It is what the SDK stores in the task and the
artifact record. Existing URLs of the form `http://<files-host>/<path>` keep working through a
legacy route, which maps `<path>` to the `local` backend.

### 3.4 Adapter interface

```python
class StorageAdapter(Protocol):
    name: str            # registry name, used in the location
    direct: bool         # True if presign_* can return a URL the client uses directly

    def presign_put(self, key: str, size: int, content_type: str | None, ttl_sec: int) -> SignedRequest: ...
    def presign_get(self, key: str, ttl_sec: int) -> SignedRequest: ...
    def head(self, key: str) -> ObjectInfo | None: ...        # size, sha256 if stored
    def put_stream(self, key: str, body: BinaryIO, size: int) -> ObjectInfo: ...   # proxied only
    def open_stream(self, key: str) -> BinaryIO: ...          # proxied only
    def delete_many(self, keys: list[str]) -> list[DeleteResult]: ...
```

`SignedRequest` holds `method`, `url`, `headers` and `expires_at`. An adapter with
`direct = False` raises on `presign_*`, and the gateway uses `put_stream` and `open_stream`
instead.

| Adapter | Direct | Notes |
|---|---|---|
| `local` | no | the current disk folder, used for artifacts |
| `s3` | yes | boto3 presigned PUT and GET; works with RustFS and AWS S3 |
| `sftp` | no | the NAS. The gateway streams it; presigning is not possible over SFTP |
| `azure` | yes | SAS URLs (phase 4) |
| `gcs` | yes | V4 signed URLs (phase 4) |

Each adapter passes one shared conformance suite: put, head, get, delete, a size mismatch
refused, an expired URL refused, a key outside the allowed prefix refused.

### 3.5 Signing rules

- A presigned URL is valid for one key and one method. The TTL is at most 900 s. The size is
  bound in the signature (S3: `content-length` in the signed headers).
- The key is built by the server from `<project>/<task>/<kind>/<name>` after the name is
  sanitised. The SDK cannot choose a prefix outside its task.
- `prepare-upload` requires write access to the task (checked with the API server, cached as the
  file server does today). `prepare-download` requires read access to the task.
- The gateway writes an audit event for each prepare call and each commit (`storage.prepare`,
  `storage.commit`), like the other endpoints of the identity layer.
- Store credentials live in secret files read by the gateway. The SDK never receives them.
- The S3 bucket is reachable only with the gateway's service user. Otherwise a user with raw keys
  could still bypass the gateway. The RustFS policy in `deploy/nas/init.py` is the place for this.

### 3.6 Changes to the SDK

- `clearml/storage/helper.py`: a `GatewayDriver` that calls `prepare-upload`, uploads to `url`
  with the given headers (plain `requests` for signed HTTP, the same flow for every direct
  adapter), then calls `commit`. Download calls `prepare-download` and follows the URL.
- The `kind` is set by the call site: `upload_artifact` sends `artifact`, `Dataset` sends
  `dataset`, `OutputModel` sends `model`, `report_image` sends `debug`. The router checks it against the
  task's tags, so a dataset task cannot write an artifact into the dataset backend by mistake.
- The default output (`development.default_output_uri`) points to the gateway in the config we
  ship. Users who set an `s3://` URI themselves keep the old direct path, and that path is only
  safe if the bucket is restricted as in 3.5.
- The `sdk.aws.s3` credential section is no longer needed for our deployment and is removed from
  the shipped config.

### 3.7 Changes to the server

- `fileserver/` becomes the gateway: new routes under `/storage/v1`, the registry loader, the
  router and the adapters in new modules (`fileserver/storage/`). The existing routes stay for
  the legacy path during migration.
- `apiserver/jobs/async_urls_delete.py`: a `gateway` storage type that calls
  `/storage/v1/delete`. The current `fileserver` type stays until migration ends.
- Metrics, events and the Elasticsearch path are untouched.

## 4. Why not an existing product

Evaluated in the chat, summarised here:

- **Filestash, SFTPGo, rclone serve s3, lakeFS** each cover one part: a file UI, a multi-backend
  server, an S3 facade, or repositories over object stores. None routes by data kind, none
  issues presigned URLs for ClearML, and none checks our IdP tokens.
- **Alluxio** has a unified namespace with mounts, but it routes by path only and is a caching
  platform. Its open-source scope is unclear.
- **MinIO** is archived and source-only, and its community console was removed.

The gateway stays ours. Adapters can reuse existing libraries (boto3, the Azure and Google
clients, paramiko or asyncssh).

## 5. Phases

| Phase | Work | Exit check |
|---|---|---|
| P0 spike | boto3 presigned PUT to RustFS; SDK-style upload with the signed URL; commit with HEAD | upload and download work with the signed URLs, and an expired or wrong-key URL is refused |
| P1 gateway core | registry loader, router, `local` adapter, `prepare-*`, `commit`, legacy route | a Task uploads an artifact through the gateway; the old URLs still download |
| P2 S3 | `s3` adapter, `GatewayDriver`, datasets and models routed to S3 | a Dataset is created and fetched with no S3 keys on the client |
| P3 SFTP | `sftp` adapter (stream only); models routed to the NAS | a model is saved to the NAS and loaded back through the gateway |
| P4 Azure, GCS | adapters with SAS and signed URLs | conformance suite passes for each |
| P5 large files | multipart presigned parts (S3), blocks (Azure), resumable (GCS) | a 5 GB file uploads and verifies |
| P6 migration | move `/mnt/fileserver` contents to `local://`, switch the default output, remove the legacy route | no task refers to the legacy host |

Each adapter is tested against a real store: RustFS for S3, an SFTP test container for SFTP,
Azurite for Azure, and fake-gcs-server for GCS.

## 6. Decisions still open

1. **Kind routing source.** The router trusts the SDK's `kind` and checks it against the task's
   tags, or it takes `kind` from the task tags alone. The second needs no SDK field, but costs
   a lookup per upload, which can be cached.
2. **Checksums.** Store `sha256` at commit, or only the size. The SDK already computes hashes for
   artifacts, so storing them is cheap.
3. **Buckets.** One bucket per backend with prefixes per kind, or one bucket per kind. One bucket
   per backend is simpler to restrict.
4. **Local copy duration.** How long the gateway keeps the `local` backend after an artifact is
   moved to a store.
5. **Web app downloads.** Keep the `302` from `/storage/v1/files/...` (no web change), or have
   the web app request signed URLs itself.
