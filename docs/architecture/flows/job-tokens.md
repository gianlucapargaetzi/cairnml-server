# Job tokens

A job never runs with the agent's own token. For each task the agent asks the server for a **job token**: a token that is bound to one task and expires with the job. The agent keeps it renewed while the job runs. The job reads it from a file.

```mermaid
sequenceDiagram
    participant Agent
    participant Server
    participant Git
    participant Job

    Agent->>Server: POST devices.job_token (task), with the agent's token
    Note over Server: check the caller is a live agent, the task is assigned to it and queued or running, owner enabled
    Note over Server: issue job token, kind job, bound to this task, 1 hour, role of the owner
    Server-->>Agent: access_token, expires_in
    Note over Agent: write the token to a 0600 file, renew it at 80 percent of its lifetime
    Agent->>Job: start the task with CAIRNML_JOB_TOKEN_FILE set
    loop while the job runs
        Job->>Server: API calls with the job token (re-read from the file)
        Agent->>Server: devices.job_token again, the file is rewritten
    end
    Job->>Server: devices.git_token (task, repository), with the job token
    Note over Server: only the task's own repository, or a registered dataset repository
    Server-->>Job: read-only installation token for the repository
    Job->>Git: clone or fetch the recorded commit
```

## Renewal

The agent renews at 80 percent of the lifetime. If a renewal fails for a network reason it tries again after 30 seconds. If the server refuses the renewal, the task has ended and the agent stops renewing.

## Endpoints

| Endpoint | Caller | Purpose |
|---|---|---|
| `devices.job_token` | agent | Issues a job token for a task the agent is running |
| `devices.git_token` | job or agent | Issues a read-only repository token. A job gets only its own task's repository, or a registered dataset repository |

## Limits

A job token acts as the task's owner: it has the owner's role, not a role limited to the task. Its lifetime is one hour, and it works only while the task is queued or running. See [Trust model](trust-model.md).
