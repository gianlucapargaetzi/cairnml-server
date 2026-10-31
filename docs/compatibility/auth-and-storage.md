# Auth and storage

CairnML changes how users sign in and how files reach the server. This page says what is canonical now and what to
do when an integration was written for the upstream behaviour.

## Sign-in and access keys

- **Sign-in is through Keycloak only.** The server has no fixed or basic sign-in mode. The web app, the SDK and the agent
  all sign in through the sign-in service at `http://<server>/cairnml/idp/`.
- **Access keys are removed.** The SDK and the agent do not read `CLEARML_API_ACCESS_KEY`, `CLEARML_API_SECRET_KEY` or
  their `CAIRNML_` equivalents, and `Session` takes no key arguments. Setting them does not sign a process in: the SDK
  reports a missing configuration (`MissingConfigError`) until the machine is enrolled.
- **Each machine has its own device key.** `cairnml init` (or `cairnml-agent init`) creates it, and the server records
  the device under the signed-in user. Revoke a machine with `cairnml logout`, or `cairnml devices revoke <id>` from
  another machine. Devices can be listed with `cairnml devices list`.
- **Automation uses a device, not a shared key.** Enrol the machine that runs the job (`cairnml init`, or `--device` on a
  machine without a browser), and keep its configuration file.

## Web sign-in and accounts

- Accounts are kept in Keycloak, in the `cairnml` realm. **Self-registration is off**: an administrator creates each
  account, and a user is a CairnML admin while they hold the `cairnml-admin` realm role.
- There is no credential page in the web app for creating keys. The Keycloak admin console is at
  `http://<server>/cairnml/idp/admin`.
- A user is disabled by switching **Enabled** off in the realm; their new sign-ins are refused, and their devices stop.

## Server sign-in modes

Upstream ClearML has fixed-user and basic sign-in modes. CairnML has no such mode: Keycloak is the only sign-in service.
Move each account to Keycloak before connecting a client, and remove the old credentials from the client's
configuration.

## File storage

Upstream ClearML serves files through the server. CairnML stores files in an S3-compatible object store (RustFS in the
shipped stack) and clients move the bytes themselves:

1. The client asks the server for an upload (`datachunks.prepare_upload`). The server records the object and returns a
   presigned `PUT` URL, or, for a large object, a multipart upload with one presigned URL per part.
2. The client uploads the bytes to the store, then calls `datachunks.commit`. The server checks the stored size and
   SHA-256 against its record and deletes a mismatching object.
3. A download asks for a presigned `GET` (`datachunks.prepare_download`).

Artifacts are stored as `datachunk://<kind>/<task>/<name>` locations. Dataset files are stored through DVC (`storage: s3`)
or in the Git LFS of the dataset repository (`storage: lfs`); see [Dataset migration](dataset-migration.md).

What changes for an integration:

- A URL that points at the server's file endpoint does not exist. Use the presigned URL the server returns, and do not
  keep it: it expires after `apiserver.storage.presign_ttl_sec` seconds.
- The storage port (`CAIRNML_STORAGE_PORT`, `9000` by default) must be reachable by the clients, because presigned URLs
  name it.
- The stack serves plain HTTP only (see the README, section "Network and security"), so presigned URLs and sign-in cookies
  travel unencrypted.
