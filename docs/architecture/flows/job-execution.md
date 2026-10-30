# Job execution

The SDK or the web app puts a task in a queue. An agent pulls it, obtains a job token for this run, fetches the source with a server-issued token, runs it and reports the result. The job itself never uses the agent's token.

## Enqueue and pull

```mermaid
sequenceDiagram
    participant SDK
    participant Server
    participant Agent

    SDK->>Server: POST tasks.enqueue (task, queue)
    Note over Server: submission check, repository on GitHub with the App installed, commit exists
    Note over Server: set status queued, add the task to the queue
    Server-->>SDK: queued 1
    Agent->>Server: POST queues.get_next_task (device token)
    Note over Server: take the highest-priority entry, a stopped task goes back to queued
    Server-->>Agent: queue entry for the task
    Agent->>Server: POST tasks.started
    Server-->>Agent: ok
```

## Run and report

```mermaid
sequenceDiagram
    participant Agent
    participant Server
    participant Git
    participant Job

    Agent->>Server: POST devices.job_token (task)
    Server-->>Agent: job token
    Agent->>Server: POST devices.git_token (task, repository)
    Server-->>Agent: read-only repository token
    Agent->>Git: clone or fetch the recorded commit
    Git-->>Agent: source tree
    Note over Agent: create the venv or Docker container, set CAIRNML_JOB_TOKEN_FILE
    Agent->>Job: start the task entry point
    loop while the job runs
        Job->>Server: events.add_batch (metrics, console lines), job token
        Server-->>Job: ok
    end
    Note over Agent: the process exits, record the exit code
    alt exit code 0
        Agent->>Server: POST tasks.completed
        Server-->>Agent: ok, status completed
    else non-zero exit
        Agent->>Server: POST tasks.failed (status message)
        Server-->>Agent: ok, status failed
    end
    Note over Agent: stop the token renewer, delete the token file
```

## Abort

```mermaid
sequenceDiagram
    participant SDK
    participant Server
    participant Agent

    SDK->>Server: POST tasks.stop (or Abort in the web app)
    Note over Server: set status stopped, the job token stops working once the task is no longer queued or running
    Server-->>SDK: ok
    Agent->>Server: GET tasks.get_all_ex (polls status)
    Server-->>Agent: status stopped
    Note over Agent: terminate the process
    Agent->>Server: POST tasks.stopped
    Server-->>Agent: ok
```

## Endpoints

| Endpoint | Caller | Purpose |
|---|---|---|
| `tasks.enqueue` | SDK, web app | Puts the task in a queue after the submission check |
| `queues.get_next_task` | agent | Takes the next task of a queue |
| `tasks.started` | agent | Marks the task as running on this agent |
| `devices.job_token` | agent | Issues the job token (see [Job tokens](job-tokens.md)) |
| `devices.git_token` | agent or job | Issues a read-only repository token |
| `events.add_batch` | job | Sends metrics and console lines |
| `tasks.completed`, `tasks.failed` | agent | Report the result |
| `tasks.stop` | SDK, web app | Asks the agent to stop the task |
| `tasks.stopped` | agent | Reports that the task was stopped |
| `tasks.get_all_ex` | agent | Reads the task status while polling |
