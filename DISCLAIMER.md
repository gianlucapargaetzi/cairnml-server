# Disclaimer

CairnML is an unofficial, modified version of ClearML. It is not created, sponsored, endorsed or supported by ClearML, Inc.

"ClearML" is a trademark of ClearML, Inc. This project uses the name only to say what the code is based on. The original copyright notices and licenses are kept (see [LICENSES.md](LICENSES.md)). The changes made to ClearML's code are recorded in each repository's history.

## What is changed in this copy

- **Dispatch** (`cairnml-server`, `cairnml-agent`): jobs wait in a queue until an agent that listens to it takes them; job containers install the agent and the SDK from local source.
- **Sign-in** (`cairnml-server`): device sign-in and directory sync through an identity provider.
- **Datasets** (`cairnml`, `cairnml-server`): git and DVC replace ClearML's dataset implementation; a dataset registry lists the repositories a job may clone.
- **SDK** (`cairnml`): `Task.enqueue` checks the server's API level rather than the level the process is using.
- **Web app** (`cairnml-web`): the interface of this copy.
- **Names and network**: everything is renamed to CairnML, and the calls to the ClearML update and statistics service are removed.
- **Tests and deployment** (`tests/e2e`, `deploy/`): the end-to-end suite and the deployment files.
