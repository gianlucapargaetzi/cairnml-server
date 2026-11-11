"""The check made when a job is submitted. On GitHub, its commit must exist, and a private repository needs the GitHub App
installed on it; a public repository needs neither the App nor a token. On another host, a private repository needs
stored Git credentials (repo_visibility). Otherwise the job is refused at once with a clear message, instead of failing
later in the agent when it tries to clone."""


from apiserver.apierrors import errors
from apiserver.idp.github import GitHubError
from apiserver.idp.access_refused import AccessRefused

# The section of the server README that sets the App up: every refusal below points to it.
GITHUB_APP_DOCS = "https://github.com/gianlucapargaetzi/cairnml-server/blob/main/README.md#github-app"






def check_submission(task) -> None:
    """Raise when the task's repository cannot be read, or its commit does not exist.

    The repository is recorded for the task's project (bll/repositories.record_use) and read as bll/repositories.resolve
    says for the user who queued the task: the project owner's assignment, else the user's own, else without a credential
    when the host allows it, else through the company's access. An unreachable repository refuses the job at once (it is
    listed Failed with the reason). A host that could not be asked never refuses it. The commit is looked up only when an
    authenticated read is used: asking GitHub for it without one would spend the anonymous rate limit.
    """
    from apiserver.bll import repositories, user_access

    repository = task.script.repository
    if not repository:
        return
    project = getattr(task, "project", None)
    repo = repositories.record_use_quietly(task.company, repository, "code", task.user, project)
    if repo is None:
        return
    root = user_access.root_project(task.company, project) if project else None
    resolution = repositories.resolve(repo, root, task.user)
    if resolution.state == repositories.UNREACHABLE:
        hint = f" Steps: {GITHUB_APP_DOCS}" if repo.host == "github.com" else ""
        raise AccessRefused(f"{repo.host}/{repo.path}: {resolution.message}{hint}")
    if repo.host != "github.com":
        return
    commit = task.script.version_num
    if not commit:
        raise errors.bad_request.ValidationError(f"no commit is recorded for {repo.path}: commit and push first")
    if resolution.entry is None or resolution.entry.kind != "app":
        return
    try:
        found = repositories._app_of(resolution.entry).commit_exists(repo.path, commit)
    except GitHubError:
        raise errors.bad_request.ValidationError("GitHub could not be reached to check the job") from None
    if not found:
        raise errors.bad_request.ValidationError(f"commit not found on GitHub: push it first ({repo.path}@{commit[:8]})")




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
