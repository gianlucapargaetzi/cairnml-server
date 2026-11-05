"""The check made when a job is submitted. On GitHub, its commit must exist, and a private repository needs the GitHub App
installed on it; a public repository needs neither the App nor a token. On another host, a private repository needs
stored Git credentials (repo_visibility). Otherwise the job is refused at once with a clear message, instead of failing
later in the agent when it tries to clone."""

from urllib.parse import urlsplit

from apiserver.apierrors import errors
from apiserver.idp.github import GitHubError, RepoNotAccessible, public_commit_exists, public_repository
from apiserver.idp.repo_visibility import check_readable
from apiserver.idp.access_refused import AccessRefused

# The section of the server README that sets the App up: every refusal below points to it.
GITHUB_APP_DOCS = "https://github.com/gianlucapargaetzi/cairnml-server/blob/main/README.md#github-app"


def repo_of(repository: str):
    """'owner/name' for a github.com repository URL, or None for anything else."""
    if not repository:
        return None
    parts = urlsplit(repository if "://" in repository else f"https://{repository}")
    if parts.netloc.lower() not in ("github.com", "www.github.com"):
        return None
    path = parts.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return path if path.count("/") == 1 else None


def public_repo(repo: str) -> bool:
    """True when GitHub shows the repository to anyone without a token. Needs no GitHub App."""
    return public_repository(_api_url(), repo)


def check_submission(task, app) -> None:
    """Raise a validation error when the task's GitHub repository or commit cannot be used.

    A public repository is cloned without a token, so it needs neither the GitHub App nor an installation; its commit
    is looked up without one. A private repository needs the App installed on it.
    """
    repo = repo_of(task.script.repository)
    if repo is None:
        if task.script.repository:
            check_readable(task.company, task.script.repository)
        return
    commit = task.script.version_num
    try:
        public = public_repo(repo)
    except GitHubError:
        raise errors.bad_request.ValidationError("GitHub could not be reached to check the job") from None
    if not public:
        if app is None:
            raise AccessRefused(
                f"GitHub is not set up: the job's repository {repo} cannot be checked without the GitHub App. "
                f"An admin adds the App in the web app (Settings > Access). "
                f"Steps: {GITHUB_APP_DOCS}")
    if not commit:
        raise errors.bad_request.ValidationError(f"no commit is recorded for {repo}: commit and push first")
    if not public:
        try:
            app.installation_token(repo)
        except RepoNotAccessible:
            raise AccessRefused(
                f"the GitHub App is not installed on {repo}: install it on this repository, with 'Only select "
                f"repositories' (steps: {GITHUB_APP_DOCS})") from None
        except GitHubError:
            raise errors.bad_request.ValidationError("GitHub could not be reached to check the job") from None
    try:
        found = app.commit_exists(repo, commit) if app is not None else public_commit_exists(
            _api_url(), repo, commit)
    except GitHubError:
        raise errors.bad_request.ValidationError("GitHub could not be reached to check the job") from None
    if not found:
        raise errors.bad_request.ValidationError(f"commit not found on GitHub: push it first ({repo}@{commit[:8]})")


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
