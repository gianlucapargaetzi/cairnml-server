# Dataset migration

Upstream ClearML datasets are addressed by an ID that the server assigns, and `clearml-data` works on that ID: create a
dataset, add files, close it (which uploads the files), and fetch it by ID. CairnML datasets are Git repositories. The
commit is the version, the repository is the address, and the files are held in DVC (in the dataset storage behind the
server) or in the Git LFS of the repository.

A script that uses dataset IDs has to change to use a repository and a commit. There is no mapping from an upstream ID to
a CairnML repository; register the existing data once and use the repository from then on.

## Command mapping

| Upstream `clearml-data` | CairnML | What changes |
|---|---|---|
| `create --project P --name N` | `cairnml dataset create N --from <folder>` (creates the GitHub repository, copies the files, checks, commits and pushes) | The repository is the dataset. Use `init` in a clone to keep an existing repository. |
| `add --files ...` | `cairnml dataset add` in the repository, after copying the files into `data/` | `add` records all of `data/`, including removed files. |
| `close` (uploads and finishes) | `cairnml dataset commit -m "..."`, then `cairnml dataset push` | `commit` checks the data first; `push` uploads the files and publishes the commit as a pull request on the default branch. |
| `upload` | `cairnml dataset push` | Files go to the dataset storage (`storage: s3`) or the Git LFS (`storage: lfs`). |
| `publish` | not needed: `push` publishes | |
| `get --id ID` | `cairnml dataset get <repository-url> <commit>` in the shell, or `Dataset.get(<repository-url>, <commit>)` in a script | Pass a commit, tag or branch. A commit is the only way to rerun the same data. |
| `list`, `sync`, `squash` | `cairnml dataset status`, `cairnml dataset cache info` | Listing across datasets is not provided; each dataset is a repository. |
| `ls` / dataset metadata by ID | the repository on GitHub and the registry | |
| (no equivalent) | `cairnml dataset doctor` | Checks the GitHub settings that publishing relies on. |
| (no equivalent) | `cairnml dataset register` | Adds the repository to the registry, once. |

## Moving an existing dataset

1. Clone or create the repository, then, inside it:

   ```bash
   cairnml dataset init --storage lfs     # or the default, s3
   cp -r /path/to/data/* data/
   cairnml dataset add
   cairnml dataset commit -m "Import from the upstream dataset <ID>"
   cairnml dataset doctor
   cairnml dataset push
   cairnml dataset register
   ```

2. In scripts, replace the dataset ID with the repository and the commit:

   ```python
   # upstream
   from clearml import Dataset
   path = Dataset.get(dataset_id="3f2a...").get_local_copy()

   # CairnML
   from cairnml.datasets import Dataset
   path = Dataset.get("https://github.com/acme/cars-data.git", "3f2a9c1").path
   ```

3. Record the commit that the training used. A run records the repository, the commit and the content hash on its own,
   so the record shows which data it used.

## Choosing the storage

`--storage s3` (the default) keeps the files in the dataset storage behind the server, through DVC. `--storage lfs` keeps
them in the GitHub LFS of the repository, with no DVC and no storage keys. The choice is made once and cannot be changed
for the repository.

## Checks that replace the upstream checks

The data is checked by `cairnml dataset check` and `commit`. The default branch changes only through pull requests that
pass the `verify` check, once it is required in the repository settings; `cairnml dataset doctor` lists what is missing.
Details are in the SDK documentation, [Datasets](https://github.com/gianlucapargaetzi/cairnml/blob/main/docs/datasets.md).
