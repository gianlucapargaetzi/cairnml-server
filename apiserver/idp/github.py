"""The GitHub App client: a repository-scoped, read-only token per job and a commit check.

Ported from the mlops server (services/github.py). This keeps what the port needs: the App token,
the installation check and the commit check. The server's UI lookups (branches, commit lists, file
reads) are not ported; they belong to our own UI, which the port replaces.
"""

import re
import time
from dataclasses import dataclass
from datetime import datetime

import jwt
import requests

_REFRESH_MARGIN = 300
_TIMEOUT = 20.0
_REPO_RE = re.compile(r"^[\w-][\w.-]*/[\w-][\w.-]*$")


class GitHubError(Exception):
    pass


_ANONYMOUS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


class RepoNotAccessible(GitHubError):
    pass


class GitHubRefused(GitHubError):
    """GitHub answered that the App ID or its key is not accepted. Not a network problem: fix the App settings."""
    pass


@dataclass(frozen=True)
class InstallationToken:
    token: str
    expires_at: datetime


def valid_repo(repo: str) -> bool:
    return bool(_REPO_RE.fullmatch(repo)) and ".." not in repo


# Public repositories need no GitHub App: they are cloned without a token. Asking GitHub whether a repository is public
# is an anonymous request, and GitHub limits those per address, so each answer is kept for a while.
_PUBLIC_TTL_SEC = 600
_public_answers: dict = {}  # repository in lower case -> (is public, when it was asked)


def _anonymous_get(api_url: str, path: str, http=None):
    try:
        return (http or requests.Session()).request(
            "GET", f"{api_url.rstrip('/')}{path}", headers=_ANONYMOUS, timeout=_TIMEOUT)
    except requests.RequestException:
        raise GitHubError("GitHub transport error") from None


def public_repository(api_url: str, repo: str, clock=time.monotonic, http=None) -> bool:
    """True when GitHub shows the repository to anyone without a token. Needs no GitHub App.
    GitHubError when GitHub cannot be asked."""
    key = repo.lower()
    cached = _public_answers.get(key)
    if cached and clock() - cached[1] < _PUBLIC_TTL_SEC:
        return cached[0]
    response = _anonymous_get(api_url, f"/repos/{repo}", http)
    if response.status_code == 404:
        answer = False
    elif response.status_code != 200:
        raise GitHubError(f"repository lookup failed: {response.status_code}")
    else:
        answer = response.json().get("private") is False
    _public_answers[key] = (answer, clock())
    return answer


def public_commit_exists(api_url: str, repo: str, sha: str, http=None) -> bool:
    """Whether GitHub has the commit of a public repository, asked without a token."""
    response = _anonymous_get(api_url, f"/repos/{repo}/commits/{sha}", http)
    if response.status_code in (404, 422):
        return False
    if response.status_code != 200:
        raise GitHubError(f"GitHub request failed: {response.status_code}")
    return True


def clear_public_cache() -> None:
    _public_answers.clear()


def repository_readable_with(api_url: str, token: str, repo: str, http=None) -> bool:
    """True when a user's own GitHub token reads the repository. GitHub answers 404 (or 403) to a token that cannot see
    a repository, so those mean no. GitHubError when GitHub cannot be asked, or answers otherwise."""
    headers = {**_ANONYMOUS, "Authorization": f"Bearer {token}"}
    try:
        response = (http or requests.Session()).request(
            "GET", f"{api_url.rstrip('/')}/repos/{repo}", headers=headers, timeout=_TIMEOUT)
    except requests.RequestException:
        raise GitHubError("GitHub transport error") from None
    if response.status_code in (401, 403, 404):
        return False
    if response.status_code != 200:
        raise GitHubError(f"repository lookup failed: {response.status_code}")
    return True


class GitHubApp:
    def __init__(self, app_id: str, private_key_pem: str, api_url: str, http=None, clock=time.time) -> None:
        # The id is a number when it comes from the environment; PyJWT needs the issuer as a string.
        self._app_id = str(app_id)
        self._key = private_key_pem
        self._api = api_url.rstrip("/")
        self._http = http or requests.Session()
        self._clock = clock
        self._installations: dict[str, int] = {}
        self._tokens: dict[str, InstallationToken] = {}
        self._public: dict[str, bool] = {}

    def _send(self, method: str, url: str, **kwargs):
        try:
            return self._http.request(method, url, timeout=_TIMEOUT, **kwargs)
        except requests.RequestException:
            raise GitHubError("GitHub transport error") from None

    def _app_jwt(self) -> str:
        now = int(self._clock())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self._app_id}, self._key, algorithm="RS256")

    @staticmethod
    def _headers(token: str) -> dict:
        return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    def _installation_id(self, repo: str) -> int:
        if repo not in self._installations:
            response = self._send("GET", f"{self._api}/repos/{repo}/installation",
                                  headers=self._headers(self._app_jwt()))
            if response.status_code == 404:
                raise RepoNotAccessible(repo)
            if response.status_code == 401:
                # GitHub does not accept the App ID or its key: not a network problem, and not fixed by retrying
                raise GitHubRefused("GitHub did not accept the App ID or its key (401)")
            if response.status_code != 200:
                raise GitHubError(f"installation lookup failed: {response.status_code}")
            installation_id = response.json().get("id")
            if not isinstance(installation_id, int):
                raise GitHubError("installation lookup returned no id")
            self._installations[repo] = installation_id
        return self._installations[repo]

    def installation_token(self, repo: str, permissions: dict = None) -> InstallationToken:
        """A short-lived token for this one repository: read access to its contents unless other permissions are given."""
        if not valid_repo(repo):
            raise GitHubError("repo must be owner/name")
        permissions = permissions or {"contents": "read"}
        key = f"{repo}|{sorted(permissions.items())}"
        cached = self._tokens.get(key)
        if cached and cached.expires_at.timestamp() - self._clock() > _REFRESH_MARGIN:
            return cached
        response = self._send(
            "POST", f"{self._api}/app/installations/{self._installation_id(repo)}/access_tokens",
            headers=self._headers(self._app_jwt()),
            json={"repositories": [repo.split("/", 1)[1]], "permissions": permissions},
        )
        if response.status_code != 201:
            raise GitHubError(f"token request failed: {response.status_code}")
        body = response.json()
        try:
            token = InstallationToken(body["token"], datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00")))
        except (KeyError, TypeError, ValueError):
            raise GitHubError("token response is malformed") from None
        self._tokens[key] = token
        return token

    def commit_exists(self, repo: str, sha: str) -> bool:
        """True when GitHub has the commit; False when it does not; GitHubError when the check itself fails."""
        response = self._send("GET", f"{self._api}/repos/{repo}/commits/{sha}", headers=self._read_headers(repo))
        if response.status_code in (404, 422):
            return False
        if response.status_code != 200:
            raise GitHubError(f"GitHub request failed: {response.status_code}")
        return True

    def app_info(self) -> dict:
        """The App itself: its name (slug) and id. Proves that the App ID and the key are accepted by GitHub."""
        response = self._send("GET", f"{self._api}/app", headers=self._headers(self._app_jwt()))
        if response.status_code in (401, 403):
            raise GitHubRefused(f"GitHub did not accept the App ID or its key ({response.status_code})")
        if response.status_code != 200:
            raise GitHubError(f"app lookup failed: {response.status_code}")
        body = response.json()
        return {"slug": body.get("slug") or "", "name": body.get("name") or "", "id": body.get("id")}

    def installed_repositories(self) -> list:
        """Every repository the App is installed on, as {repository, default_branch, private}.

        The installations are listed with the App's own token; the repositories of each one with a token for that
        installation. Each page is one request of up to 100 items.
        """
        repos = []
        for installation in self._pages(f"{self._api}/app/installations", self._headers(self._app_jwt())):
            minted = self._send(
                "POST", f"{self._api}/app/installations/{installation['id']}/access_tokens",
                headers=self._headers(self._app_jwt()), json={"permissions": {"contents": "read"}})
            if minted.status_code != 201:
                raise GitHubError(f"token request failed: {minted.status_code}")
            headers = self._headers(minted.json()["token"])
            for item in self._pages(f"{self._api}/installation/repositories", headers, key="repositories"):
                repos.append({
                    "repository": item["full_name"],
                    "default_branch": item.get("default_branch") or "",
                    "private": item.get("private"),
                })
        return repos

    def _pages(self, url: str, headers: dict, key: str = None) -> list:
        items, page = [], 1
        while True:
            response = self._send("GET", url, headers=headers, params={"per_page": "100", "page": str(page)})
            if response.status_code != 200:
                raise GitHubError(f"listing failed: {response.status_code}")
            body = response.json()
            batch = (body.get(key) if key else body) or []
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1

    # What dataset access needs: reading a repository's files and folders.

    def _is_public(self, repo: str) -> bool:
        """True when GitHub shows the repository to anyone without a token. Such a repository is read without the App."""
        if repo not in self._public:
            response = self._send("GET", f"{self._api}/repos/{repo}", headers=_ANONYMOUS)
            self._public[repo] = response.status_code == 200 and response.json().get("private") is False
        return self._public[repo]

    def _read_headers(self, repo: str) -> dict:
        if self._is_public(repo):
            return _ANONYMOUS
        return self._headers(self.installation_token(repo).token)

    def default_branch_head(self, repo: str) -> tuple:
        """(default branch, the sha of its latest commit)."""
        response = self._send("GET", f"{self._api}/repos/{repo}", headers=self._read_headers(repo))
        if response.status_code != 200:
            raise GitHubError(f"repository lookup failed: {response.status_code}")
        branch = response.json().get("default_branch")
        commit = self._send("GET", f"{self._api}/repos/{repo}/commits/{branch}", headers=self._read_headers(repo))
        if commit.status_code != 200:
            raise GitHubError(f"branch lookup failed: {commit.status_code}")
        return branch, commit.json()["sha"]

    def read_file(self, repo: str, path: str, ref: str = None):
        """The bytes of a file at a ref (the default branch if None), or None when there is no such file."""
        headers = dict(self._read_headers(repo), Accept="application/vnd.github.raw+json")
        response = self._send("GET", f"{self._api}/repos/{repo}/contents/{path}", headers=headers,
                              params={"ref": ref} if ref else None)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise GitHubError(f"file read failed: {response.status_code}")
        return response.content

    def tree_sizes(self, repo: str, ref: str) -> dict:
        """Size in bytes of every file in the repository at a ref, by path."""
        response = self._send("GET", f"{self._api}/repos/{repo}/git/trees/{ref}", headers=self._read_headers(repo),
                              params={"recursive": "1"})
        if response.status_code != 200:
            raise GitHubError(f"tree lookup failed: {response.status_code}")
        body = response.json()
        if body.get("truncated"):
            raise GitHubError("the repository has too many files to list")
        return {item["path"]: item.get("size", 0) for item in body.get("tree", []) if item.get("type") == "blob"}

    def list_dir(self, repo: str, path: str, ref: str = None) -> list:
        """The names of the files in a folder, or an empty list when there is none."""
        response = self._send("GET", f"{self._api}/repos/{repo}/contents/{path}", headers=self._read_headers(repo),
                              params={"ref": ref} if ref else None)
        if response.status_code == 404:
            return []
        if response.status_code != 200:
            raise GitHubError(f"folder listing failed: {response.status_code}")
        return [item["name"] for item in response.json() if item.get("type") == "file"]

    def read_dir_texts(self, repo: str, path: str) -> dict:
        """The text of every file in a folder of the default branch, by name, in one request.

        Reading a folder file by file takes one request per file; this takes one however many files there are.
        Returns an empty dict when there is no such folder. Files that are not text, or too large to come back whole,
        are left out.
        """
        if self._is_public(repo):
            # The GraphQL endpoint needs a token; a public repository is read file by file instead.
            texts = {}
            for name in self.list_dir(repo, path):
                content = self.read_file(repo, f"{path}/{name}")
                if content is not None:
                    try:
                        texts[name] = content.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
            return texts
        owner, name = repo.split("/", 1)
        query = (
            "query($owner: String!, $name: String!, $expr: String!) { repository(owner: $owner, name: $name) "
            "{ object(expression: $expr) { ... on Tree { entries { name type object { ... on Blob { text isTruncated } } } } } } }"
        )
        response = self._send(
            "POST", f"{self._api}/graphql", headers=self._read_headers(repo),
            json={"query": query, "variables": {"owner": owner, "name": name, "expr": f"HEAD:{path}"}},
        )
        if response.status_code != 200:
            raise GitHubError(f"folder read failed: {response.status_code}")
        body = response.json()
        if body.get("errors"):
            raise GitHubError("folder read failed: " + "; ".join(e.get("message", "?") for e in body["errors"]))
        tree = ((body.get("data") or {}).get("repository") or {}).get("object")
        if not tree:
            return {}
        texts = {}
        for entry in tree.get("entries") or []:
            blob = entry.get("object") or {}
            if entry.get("type") == "blob" and blob.get("text") is not None and not blob.get("isTruncated"):
                texts[entry["name"]] = blob["text"]
        return texts

    def list_commits(self, repo: str, ref: str, limit: int) -> list:
        """The latest commits of a ref, newest first: sha, first line of the message, author, date and parent shas."""
        response = self._send("GET", f"{self._api}/repos/{repo}/commits", headers=self._read_headers(repo),
                              params={"sha": ref, "per_page": str(min(limit, 100))})
        if response.status_code != 200:
            raise GitHubError(f"commit list failed: {response.status_code}")
        commits = []
        for item in response.json():
            details = item.get("commit") or {}
            author = details.get("author") or {}
            commits.append({
                "sha": item["sha"],
                "message": ((details.get("message") or "").splitlines() or [""])[0],
                "author": author.get("name") or "",
                "date": author.get("date") or "",
                "parents": [parent["sha"] for parent in item.get("parents") or []],
            })
        return commits


class TokenReader(GitHubApp):
    """Reads a repository with a personal token, through the same read calls as the App. Only reads: it issues no token
    of its own. Used where a repository is reached through a token shared with a project, and its layout is checked."""

    def __init__(self, token: str, api_url: str, http=None, clock=time.time) -> None:
        super().__init__("0", "", api_url, http=http, clock=clock)
        self._token = token

    def _read_headers(self, repo: str) -> dict:
        return self._headers(self._token)


class PublicReader(GitHubApp):
    """Reads public repositories without a token or a GitHub App. Used when the server has no App: the public dataset
    registries and public datasets are still listed. A private repository is refused, as the App is not there to read it."""

    def __init__(self, api_url: str, http=None, clock=time.time) -> None:
        super().__init__("0", "", api_url, http=http, clock=clock)

    def _read_headers(self, repo: str) -> dict:
        if self._is_public(repo):
            return _ANONYMOUS
        raise RepoNotAccessible(f"{repo} is private, and the server has no GitHub App to read it")
