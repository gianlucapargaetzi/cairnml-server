# CLI migration

Upstream ClearML installs one command per tool (`clearml-init`, `clearml-task`, and so on). CairnML installs one command,
`cairnml`, with a subcommand for each tool. The subcommand takes the arguments of the tool it names. A script that calls
the upstream command must change its command name, and in most cases nothing else.

No upstream command name is installed as an alias. Scripts that call `clearml-*` fail with "command not found" until
they are changed.

| Upstream | CairnML | Notes |
|---|---|---|
| `clearml-init` | `cairnml init <server>` | Signs in with this machine's device key. No access key is asked for. See [enrolment](#enrolment). |
| `clearml-task` | `cairnml task ...` | The same options for the project, name, repository, branch, commit, script, queue and requirements. |
| `clearml-data` | `cairnml dataset ...` | Different workflow; see [Dataset migration](dataset-migration.md). |
| `clearml-param-search` | `cairnml param-search ...` | |
| `clearml-debug` | `cairnml debug ...` | `cairnml debug token` and `cairnml debug config dump`. |
| `clearml-agent` | `cairnml-agent ...` | A separate package. See [agent](#agent). |

## Enrolment

```bash
cairnml init http://<server-ip>       # opens the sign-in page; prints "enrolled as ..." when done
cairnml whoami                        # who this machine signs in as
cairnml doctor                        # the configuration, the server, the sign-in and the key
cairnml logout                        # revokes this machine's enrolment and removes its key
```

On a machine with no browser, add `--device`: the command shows a short code and an address to open on another device.
`cairnml devices list` and `cairnml devices revoke <id>` manage the devices enrolled under your sign-in.

Replace the upstream `CLEARML_API_HOST`, `CLEARML_API_ACCESS_KEY` and `CLEARML_API_SECRET_KEY` with the enrolment. The
access key and secret are not read at all (see [Auth and storage](auth-and-storage.md#sign-in-and-access-keys)).

## Tasks

```bash
cairnml task --project examples --name train --repo https://github.com/acme/model.git --script train.py --queue default
```

The options are the same names as `clearml-task`. Anything the upstream tool took from `clearml.conf` is read from
`~/cairnml.conf` instead, and the configuration file is written by `cairnml init`.

## Parameter search

```bash
cairnml param-search ...
```

Takes the arguments of `clearml-param-search`. Run `cairnml param-search --help` for the current list.

## Debugging

`clearml-debug` is `cairnml debug`. Use `cairnml debug config dump` in place of printing the upstream configuration,
and `cairnml debug token` to show the details of the current token.

## Agent

The agent is a separate package with its own command:

```bash
cairnml-agent init                    # enrols the agent with the server
cairnml-agent --help                  # the commands of the agent
```

Its settings are read from `~/cairnml.conf` and `~/cairnml-agent.conf`, and the environment names are `CAIRNML_AGENT_*`. The older `TRAINS_*`
names are still read and print a deprecation message once.

## Python import

```python
# upstream
from clearml import Task

# CairnML
from cairnml import Task
```

The `Task` methods used by training scripts (`Task.init`, logging, `connect`, artifacts) keep their names. The dataset
classes are in `cairnml.datasets`; see [Dataset migration](dataset-migration.md#use-a-dataset-in-a-script).

## Configuration file

| Upstream | CairnML |
|---|---|
| `~/clearml.conf` | `~/cairnml.conf` (written by `cairnml init`) |
| `CLEARML_CONFIG_FILE` | `CAIRNML_CONFIG_FILE` |

`~/trains.conf` and `TRAINS_CONFIG_FILE` are still read, so an older installation keeps working. Move the settings to the
new name when you change the machine.
