# Flows

How the parties of CairnML talk to each other for the common operations. Each page has one sequence diagram (Mermaid, rendered by GitHub) and the endpoints it names.

| Page | What it covers |
|---|---|
| [Sign-in](sign-in.md) | Web sign-in with PKCE, the session cookies, authenticated requests, logout |
| [Device enrolment](device-enrolment.md) | Enrolling an SDK machine or an agent with its own key |
| [Token exchange](token-exchange.md) | Turning a device key into a short-lived access token |
| [Job tokens](job-tokens.md) | The per-job token, its renewal, and the repository token |
| [Job execution](job-execution.md) | From enqueue to a reported result on an agent |
| [Task logging](task-logging.md) | Events, console output and artifact upload |
| [Database management](database-management.md) | Start-up, migrations, record writes and object removal |
| [Trust model](trust-model.md) | Who may act for whom, and the open weaknesses |

Participants: **SDK** (Python client on a user's machine), **Server** (CairnML API), **IDP** (Keycloak), **Agent** (runs jobs), **WebUI** (browser app), **RustFS** (object storage), **Git** (source repositories).

Lifetimes in one place:

| Credential | Lifetime | Issued by |
|---|---|---|
| Web session token (cookie) | 15 minutes | `login.idp_callback` |
| Device access token | 15 minutes | `devices.exchange` |
| Job token | 1 hour, renewed by the agent | `devices.job_token` |
| Device assertion | at most 5 minutes, used once | the device itself |
| Presigned storage URL | 15 minutes | `datachunks.prepare_upload` |
