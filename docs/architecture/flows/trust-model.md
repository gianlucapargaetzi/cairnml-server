# Trust model

This page says who may act for whom, and where the current implementation is weaker than the design intends.

## Principals

| Principal | Acts for | Credential | Lifetime |
|---|---|---|---|
| A person (Keycloak user) | Themselves in the web app and the SDK | Keycloak session, then the web session token | 15 minutes per token |
| A device (SDK machine or agent) | Its owner, the person who enrolled it | Device key, exchanged for an access token | Until revoked; tokens 15 minutes |
| A job | The owner of the task | Job token | 1 hour, renewed while the job runs |
| An admin | Everything | The `cairnml-admin` realm role | As for the person |

## Rules that hold

- Any signed-in user may enrol an agent. The agent then holds that user's role, not an admin's: its jobs and queues follow the rules for that user.
- A queue is made only when an agent is enrolled: it is the agent's own queue, named after its machine, and the user who enrolled the agent owns it. Nobody creates a queue by hand. A user sees a queue when they own it, hold a grant on it, are a member of a root project it is allocated to, or when it is public. Admins see every queue. Queues created before ownership have no owner and stay admin-managed.
- The owner manages a queue (update, delete, grants and metadata); the owner and admins may set its `public` flag. Any user's agent may serve a public queue. A grantee uses the queue but cannot manage it.
- An agent may serve a queue only when the queue is public, its user owns it, holds a grant on it, or the user is an admin. Agent tokens cannot create, update, delete or manage the grants of queues.
- A device or job token stops working when its device is revoked, its owner is disabled, or (for a job) its task is no longer queued or running.
- Each device assertion is bound to one server address and can be used once.
- The server never returns a stored Git secret. It issues short-lived, read-only repository tokens instead.
- Own datasets live in their own repositories and are not registered anywhere. Discovery and access follow the GitHub App's installations: the server lists the repositories the App is installed on, and a job may get a token for any one of them that has a `dataset.yaml` at its root and a footprint that holds. An installed repository without `dataset.yaml` gets no token.
- The dataset list is a cache. Listing it reads the snapshot and never calls GitHub. The snapshot is rebuilt once an hour, and the entry of a dataset is refreshed when a job token is issued for it and the entry is missing or older than an hour. A job token is never answered from the cache: the dataset is verified against GitHub every time. A repository leaves the list only when GitHub confirms it is no longer a dataset; a transient GitHub failure keeps the last good entry.
- A public repository gets no token: the server answers a job's token request for it with success and `token: null`, and the job clones it anonymously. The App is not asked for it, and it is not needed for it.
- The dataset registry holds public datasets and adapters only. A registry entry with `source: repo` is ignored.
- Disabling a person in Keycloak takes effect within about a minute, because the server synchronises users.
- Every enabled person of the realm gets a CairnML user within about a minute, without signing in first, so the add-user lists of the web app show them. Disabled people get none.

## Known weaknesses

These are tracked as issues. The table names the place where the weakness is.

| Weakness | Where | Issue |
|---|---|---|
| In venv mode, job code runs as the agent's user and can read the agent's device key, which carries the enrolling admin's role | Agent | gianlucapargaetzi/cairnml-agent#12 |
| Docker arguments from a task are only filtered for `--entrypoint`, so a task can ask for privileged containers | Agent | gianlucapargaetzi/cairnml-agent#13 |
| A job token has the owner's role, not a role limited to its task | Server | #31 |
| The server takes its public address from the request's Host header | Server | #32 |
| Keycloak runs in development mode, and its admin console is reachable through the proxy | Server | #33 |
| The stack runs over plain HTTP, so cookies and tokens are not encrypted on the network | Server | #34 |
| Disabling a person stops all devices they own, with no warning and no way to hand them over | Server | #46 |

Until the first two are fixed, treat agent hosts as belonging to the people who may submit jobs to them.
