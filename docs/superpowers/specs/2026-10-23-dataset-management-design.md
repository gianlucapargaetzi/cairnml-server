# Dataset management: design

Status: approved plan; two open questions remain at the end (sample format, default cache size).

## Intent

Training runs must be reproducible and traceable against the exact data they used, without putting large files into git and without redownloading data that a machine already has.

Today the training pipeline logs a folder path and a timestamp. It cannot show what data a run saw, and it does not notice when the data changes. Datasets range from small CSV time series to multi-GB image sets, and they change whenever new measurements arrive or a supplier sends more.

## Goals

1. Local datasets are versioned by git commit. Git holds metadata only. File content is stored in object storage through DVC.
2. A simple command-line workflow for local datasets: `init`, `status`, `add` (including deletions), `commit`, `push`. The tool drives git and DVC, so the user does not run them directly.
3. External datasets (COCO, Hugging Face, FiftyOne, and others) are linked by source and read through adapters. A run uses the source's own revision whenever the source has one (a Hugging Face commit sha, a release tag), and any revision its adapter can read.
4. Each machine keeps a local cache of datasets, with a size limit set in that machine's local configuration. A dataset already in the cache is not downloaded again.
5. A training run names its dataset as a link plus a commit. The run records the commit it actually used, and for external datasets the adapter name it used.
6. Adapters are found automatically from one adapter repository. A dataset may have a default adapter and per-commit overrides, for when the source structure changes.

## Non-goals

- Access control between users. The GitHub App gives the tool access to the dataset repositories, and that is all.
- Changes to the CairnML web UI for datasets.
- Hyperdatasets (the `datachunks` and `hyper_dataset` features). They are a separate system and are not touched.
- Automating FiftyOne export. The first adapter for FiftyOne may be written by hand.

## Anti-goals

These would make the design a failure even if it runs:

- Large files in git, or any file content committed to a git repository.
- A run that silently uses a different commit or adapter than the one it reports.
- A deletion that is not recorded as a change, so the dataset looks the same after files were removed.
- A cache that grows past its limit, or that is used without checking the content hash.
- A dataset that works on one machine and fails on another because the cache or the adapter differs.

## Constraints

- Server host: Linux, Docker, the CairnML stack from this repository. Object storage is the RustFS service with a `dataset` bucket.
- Agents and users reach object storage through presigned S3 URLs, as the architecture doc describes.
- The owner installs the CairnML GitHub App in each dataset repository, so the tool can reach them.
- The existing `cairnml-data` command goes away. The owner has no CairnML datasets yet, so there is no migration.
- Training uses the CairnML SDK for task tracking, so run records stay in CairnML.

## Design

### Local datasets

- One git repository per local dataset. It holds the metadata and the DVC pointer files. The `data/` folder is tracked by DVC.
- Content is pushed to a DVC remote, which is the `dataset` bucket in RustFS.
- The git commit of the dataset repository is the dataset version.
- `status` shows the current branch, the staged and unstaged changes, and untracked files.
- `add` accepts new, changed and deleted paths. A deletion is recorded in the commit.
- `commit -m` commits the DVC pointer files and the git change together, as one step.
- `push` sends the content to the DVC remote and the commits to the git remote.

### External datasets

- One repository, `external-datasets`, holds the adapter code and one small pin file per dataset.
- A pin file holds the source link, the default adapter name, and optional overrides: a list of commits or commit ranges, each mapped to a different adapter.
- A commit of `external-datasets` fixes the pin file and the adapter code. The source's own revision is chosen when a training run starts: requested by the run, else the pin file's optional `revision`, else the latest. The resolved revision is always recorded.
- Sources with no revision concept (plain URLs) are recorded by the content hash of what was read.
- Adapter selection, in order: an override in the pin file that matches the source revision, then the default adapter.
- Each adapter checks the data when it loads. If the layout does not match what the adapter expects, it fails with an error that names the adapter and the commit.
- Each adapter has tests that run against a list of known good commits.

### Cache

- The cache lives at a path set in the machine's local configuration, with a size limit in bytes.
- Entries are keyed by content hash for local datasets, and by source link plus commit for external datasets.
- When the limit is reached, the least recently used entries are removed. Entries in use by a running task are not removed.

### Training runs

- A run names its dataset as `local:<repo>@<commit>` or `external:<name>@<external-datasets-commit>[#<source-revision>]`.
- The run records in CairnML: the repository, the commit, the content hash for local datasets, and for external datasets the source revision, the adapter name and the adapter's `external-datasets` commit.
- If the commit cannot be resolved, is not pushed, or the adapter rejects the data, the run fails before training starts.

### Access

- DVC reaches the `dataset` bucket with short-lived keys limited to that bucket, issued by a new server endpoint through RustFS STS. If RustFS cannot scope them as needed, the fallback is per-user RustFS users limited to `dataset/*`.
- People use their own git login for dataset repositories. Agents get a read-only GitHub App token from the server, for the dataset repositories named by the task and for `external-datasets`.

### Canonical sample format

Every adapter writes the same shape: one row per sample, with a sample id, a split, the modalities (for example image and depth, as paths or values), and the labels (for example class, bounding boxes, masks). The exact file format is an open question below.

## What is left to the builder

- The CLI's name and its exact command syntax, within the workflow above.
- The local configuration file format and the default cache limit.
- How the cache finds entries that are in use by a running task.
- The layout of the external-datasets repository and the adapter interface.
- How the pipeline locates the adapter for a given commit.
- The error messages.

## Open questions for the owner

1. Canonical sample format: a home-built table in Parquet, or the Hugging Face `datasets` schema adapted to our needs? My recommendation is the home-built Parquet table, with the field names chosen to match Hugging Face where it makes sense.
2. Default cache limit. The cache is per user, with the limit set per machine in that user's configuration.

## Calls made without the owner

- The git commit is the dataset version.
- DVC is the content layer, with the RustFS `dataset` bucket as its remote.
- Per-commit adapter overrides are listed in the external dataset's pin file.
- Adapter checks run at load time, plus tests against a list of commits.

## Spike results (2026-10-23)

- **Scoped temporary keys work on RustFS.** The server's service user can call STS `AssumeRole` with an inline session policy. The returned key, secret and session token are limited to what the session policy allows on top of what the service user already has (an intersection), and DVC `push` and `pull` work with them against the `dataset` bucket.
- **Policy rules found.** `sts:AssumeRole` must sit in its own statement (RustFS refuses mixed action families), with the resource `arn:aws:s3:::*`. The service user needs `s3:ListBucket`, `s3:GetBucketLocation` on the `dataset` bucket and the multipart actions on `dataset/*`, or the temporary keys cannot list either.
- **DVC cache.** A shared DVC cache with hardlinks lets several checkouts share one copy of each file, and an older commit can be restored from the remote. A file's link count cannot tell whether our cache still uses it, because a user's own DVC workspace adds links too, so eviction has to track the files our own checkouts reference.
