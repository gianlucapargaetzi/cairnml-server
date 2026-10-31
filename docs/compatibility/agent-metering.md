# Agent metering and proxy: not supported

**Decision: not planned.** CairnML does not include the upstream agent subsystem for app metering, proxying and wrapping
of desktop and command-line app traffic. It is not in the agent, and no setting turns it on.

## Context

Upstream ClearML has a dedicated app metering, proxy and wrapper subsystem for the traffic of certain desktop and command-line
applications. CairnML's agent does not expose it. Deployments that were built on it therefore have no equivalent here.

## Consequences

- Deployments that depend on the upstream traffic metering or the per-application policy cannot reproduce it on CairnML.
- The agent does not record per-application traffic, and there is no CairnML API for it.
- Nothing is silently accepted in its place: the agent has no configuration key for metering, so none can be set and
  left without effect.

## Alternatives

- Meter at the network: run the agent's jobs on a network segment whose traffic is measured by the network or a proxy you
  already operate.
- Restrict egress with the container runtime or the host firewall, for example by allowing only the Git hosts and the
  CairnML server.
- Record run-level status and timing: the server keeps them for each run.

## Revisiting the decision

A change of this decision needs a product request, a new row in the [compatibility matrix](matrix.md) and a change to
this page. Until then the status stays `not planned`.
