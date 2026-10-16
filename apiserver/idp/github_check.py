"""The check made when a job is submitted: its commit must exist on GitHub, and the GitHub App must
be installed on its repository. Otherwise the job is refused at once with a clear message, instead of
failing later in the agent when it tries to clone."""

from urllib.parse import urlsplit

from apiserver.apierrors import errors
from apiserver.idp.github import GitHubError, RepoNotAccessible


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


def check_submission(task, app) -> None:
    """Raise a validation error when the task's GitHub repository or commit cannot be used."""
    repo = repo_of(task.script.repository)
    if repo is None or app is None:
        return
    commit = task.script.version_num
    if not commit:
        raise errors.bad_request.ValidationError(f"no commit is recorded for {repo}: commit and push first")
    try:
        app.installation_token(repo)
    except RepoNotAccessible:
        raise errors.bad_request.ValidationError(f"the GitHub App is not installed on {repo}") from None
    except GitHubError:
        raise errors.bad_request.ValidationError("GitHub could not be reached to check the job") from None
    try:
        found = app.commit_exists(repo, commit)
    except GitHubError:
        raise errors.bad_request.ValidationError("GitHub could not be reached to check the job") from None
    if not found:
        raise errors.bad_request.ValidationError(f"commit not found on GitHub: push it first ({repo}@{commit[:8]})")


def build_from_config():
    """The App client from configuration and the secrets mount; None when no App is configured."""
    from apiserver.config_repo import config
    from apiserver.idp.github import GitHubApp
    from apiserver.idp.secrets import read_secret

    app_id = config.get("apiserver.idp.github_app_id", "")
    if not app_id:
        return None
    return GitHubApp(app_id, read_secret("github_app_key"), config.get("apiserver.idp.github_api_url"))
