# Compatibility with upstream ClearML

CairnML is based on ClearML and has changed it in several places on purpose. This folder says which upstream behaviour
CairnML keeps, which it changes, which it removed and what to use instead.

- [Compatibility matrix](matrix.md): one row per area, with its status and the migration path. This is the list the
  tests check.
- [CLI migration](cli-migration.md): the upstream commands and their CairnML equivalents, and the Python import.
- [Dataset migration](dataset-migration.md): the ID-based dataset workflow and the repository-based one.
- [Auth and storage](auth-and-storage.md): sign-in, access keys, accounts, and how files reach the object storage.
- [Agent metering](agent-metering.md): the decision on the upstream app metering and proxy path (not supported).

## Policy

1. **Kept.** Behaviour that a script or a training run relies on for its results is kept where the design allows it:
   `Task.init` and the logging calls, the agent's job execution, and the Git-based code and requirements capture.
2. **Changed.** A name, a command or a storage path differs. The migration path is in the matrix row. Old names are
   reported and not silently ignored where the code can tell (for example the legacy `TRAINS_*` environment names are
   still read, with a deprecation message).
3. **Removed or not planned.** The feature is not in CairnML and is not going to be added in this line. The matrix says
   so, and the migration column names the alternative. Nothing is silently accepted in its place.
4. **No compatibility shims for removed credentials.** Access keys and the fixed or basic sign-in modes are not read.
   Use `cairnml init` on each machine instead.
5. **Versions.** Each component has its own version number. The pins in `docker/build/` name the commits that work
   together; see the README section "Compatibility".

Changes to this policy need an entry in the matrix first.
