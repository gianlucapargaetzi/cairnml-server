// 4) Job execution on an agent: enqueue, pull, job token, source, run, report.
#import "_chronos.typ": *


The SDK or the web app puts a task in a queue. An agent pulls it, obtains a token for this job alone, fetches the source with a server-issued Git credential, runs it and reports the result. The job never uses the agent's own token.

#block(breakable: false)[
= Job execution on an agent
== Enqueue and pull

#seq-diagram(("SDK", "Server", "Agent"), (
  call("SDK", "Server", "POST tasks.enqueue (task, queue)"),
  process("Server", "Check the repository and commit for submission; set status queued; add the task to the queue"),
  reply("Server", "SDK", "queued: 1"),
  call("Agent", "Server", "POST queues.get_next_task (device token)"),
  process("Server", "Pop the highest-priority entry; a stopped task goes back to queued"),
  reply("Server", "Agent", "queue entry for the task"),
  call("Agent", "Server", "POST tasks.started"),
  reply("Server", "Agent", "ok"),
))
]

#block(breakable: false)[
== Job token and source

#seq-diagram(("Agent", "Server", "Git"), (
  call("Agent", "Server", "POST devices.job_token (task)"),
  process("Server", "Check the caller is a live agent, the task is assigned to it and queued or running, and its owner is enabled; issue a job token for the owner, bound to this task, 3600 s"),
  reply("Server", "Agent", "access_token, expires_in"),
  process("Agent", "Write the token to a 0600 file; renew it at 80% of its lifetime"),
  call("Agent", "Server", "POST devices.git_token (task, repository)"),
  process("Server", "Check the repository is the task's own or a registered dataset repository; create a read-only GitHub App token"),
  reply("Server", "Agent", "token, username x-access-token"),
  call("Agent", "Git", "clone or fetch the recorded commit (token via askpass)"),
  reply("Git", "Agent", "source tree"),
))
]

#block(breakable: false)[
== Running and reporting

#seq-diagram(("Agent", "Server", "RustFS"), (
  process("Agent", "Create the venv or Docker container; set CAIRNML_JOB_TOKEN_FILE"),
  process("Agent", "Start the task entry point; stream its output to the task console"),
  call("Agent", "Server", "POST events.add_batch (console and status, job token)"),
  reply("Server", "Agent", "ok"),
  process("Agent", "The process exits: record the exit code and stop signal"),
  alt((
    (guard: "exit code 0", steps: (
      call("Agent", "Server", "POST tasks.completed"),
      reply("Server", "Agent", "ok; status completed"),
    )),
    (guard: "non-zero exit", steps: (
      call("Agent", "Server", "POST tasks.failed (status message)"),
      reply("Server", "Agent", "ok; status failed"),
    )),
  )),
  process("Agent", "Stop the token renewer; delete the token file"),
))
]

#block(breakable: false)[
== Abort

#seq-diagram(("SDK", "Server", "Agent"), (
  call("SDK", "Server", "POST tasks.stop (or Abort in the web app)"),
  process("Server", "Set status stopped; the job token stops working once the task is no longer queued or running"),
  reply("Server", "SDK", "ok"),
  call("Agent", "Server", "GET tasks.get_all_ex (poll status)"),
  reply("Server", "Agent", "status stopped"),
  process("Agent", "Terminate the process; report the task as stopped"),
))
]
