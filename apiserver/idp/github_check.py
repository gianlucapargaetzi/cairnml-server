"""The check made when a job is submitted. On GitHub, its commit must exist, and a private repository needs the GitHub App
installed on it; a public repository needs neither the App nor a token. On another host, a private repository needs
stored Git credentials (repo_visibility). Otherwise the job is refused at once with a clear message, instead of failing
later in the agent when it tries to clone."""


from apiserver.apierrors import errors
from apiserver.idp import repo_address
from apiserver.idp.github import GitHubError, public_repository
from apiserver.idp.access_refused import AccessRefused

# The section of the server README that sets the App up: every refusal below points to it.
GITHUB_APP_DOCS = "https://github.com/gianlucapargaetzi/cairnml-server/blob/main/README.md#github-app"


def repo_of(repository: str):
    """'owner/name' for a github.com repository address in any spelling (https, ssh, git, with or without .git, a user, a
    port or www.), or None for anything else. The case is kept as written; apiserver.idp.repo_address has the identity."""
    parsed = repo_address.parse(repository)
    return parsed.path if parsed is not None and parsed.is_github else None


def public_repo(repo: str) -> bool:
    """True when GitHub shows the repository to anyone without a token (the REST API; still used by the dataset paths,
    which move to the git discovery request in the datasets phase)."""
    return public_repository(_api_url(), repo)


def check_submission(task) -> None:
    """Raise when the task's repository cannot be read, or its commit does not exist.

    The repository is recorded for the task's project (bll/repositories.discover) and read as bll/repositories.resolve
    says: the assigned credential alone, "None needed", or, with nothing assigned, without a credential when the host
    allows it, else through the company's access. An unreachable repository refuses the job at once (it is listed
    Failed with the reason). A host that could not be asked never refuses it. The commit is looked up only when an
    authenticated read is used: asking GitHub for it without one would spend the anonymous rate limit.
    """
    from apiserver.bll import repositories

    repository = task.script.repository
    if not repository:
        return
    binding = repositories.discover(task.company, repository, task.user, getattr(task, "project", None))
    if binding is None:
        return
    source = repositories.source_of(binding)
    resolution = repositories.resolve(source, binding)
    if resolution.state == repositories.UNREACHABLE:
        hint = f" Steps: {GITHUB_APP_DOCS}" if source.host == "github.com" else ""
        raise AccessRefused(f"{source.host}/{source.path}: {resolution.message}{hint}")
    if source.host != "github.com":
        return
    commit = task.script.version_num
    if not commit:
        raise errors.bad_request.ValidationError(f"no commit is recorded for {source.path}: commit and push first")
    app = repositories.github_client(resolution, source)
    if app is None:
        return
    try:
        found = app.commit_exists(source.path, commit)
    except GitHubError:
        raise errors.bad_request.ValidationError("GitHub could not be reached to check the job") from None
    if not found:
        raise errors.bad_request.ValidationError(f"commit not found on GitHub: push it first ({source.path}@{commit[:8]})")


def _api_url() -> str:
    from apiserver.config_repo import config

    return config.get("apiserver.idp.github_api_url")


def build_from_config():
    """The server's GitHub App client, from the company access an admin added in the UI (Settings > Access); None when
    no admin has added one. Its ID and key are kept in the database, encrypted, not in the configuration."""
    from apiserver.bll.user_access import server_app
    from apiserver.config_repo import config
    from apiserver.idp.github import GitHubApp

    found = server_app()
    if found is None:
        return None
    entry, key = found
    return GitHubApp(entry.app_id, key, entry.api_url or config.get("apiserver.idp.github_api_url"))


def reader():
    """What the Datasets pages read through: the server's GitHub App when there is one, otherwise a reader of public
    repositories (no App needed). Private repositories then fail with GitHubError, as they cannot be reached."""
    from apiserver.config_repo import config
    from apiserver.idp.github import PublicReader
    from apiserver.idp.secrets import SecretMissing

    try:
        app = build_from_config()
    except SecretMissing:
        app = None
    return app or PublicReader(config.get("apiserver.idp.github_api_url"))
