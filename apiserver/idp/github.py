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


class RepoNotAccessible(GitHubError):
    pass


@dataclass(frozen=True)
class InstallationToken:
    token: str
    expires_at: datetime


def valid_repo(repo: str) -> bool:
    return bool(_REPO_RE.fullmatch(repo)) and ".." not in repo


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
        token = self.installation_token(repo).token
        response = self._send("GET", f"{self._api}/repos/{repo}/commits/{sha}", headers=self._headers(token))
        if response.status_code in (404, 422):
            return False
        if response.status_code != 200:
            raise GitHubError(f"GitHub request failed: {response.status_code}")
        return True

    # What dataset registration needs: reading a repository's files, and a pull request into the registry.

    def _read_headers(self, repo: str) -> dict:
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

    def branch_exists(self, repo: str, branch: str) -> bool:
        response = self._send("GET", f"{self._api}/repos/{repo}/git/ref/heads/{branch}", headers=self._read_headers(repo))
        if response.status_code == 404:
            return False
        if response.status_code != 200:
            raise GitHubError(f"branch lookup failed: {response.status_code}")
        return True

    def open_registration_pull_request(self, repo: str, branch: str, path: str, content: str, message: str,
                                       title: str, body: str) -> str:
        """Add one file to the repository through a new branch and a pull request, with auto-merge. Returns the PR URL.

        Needs the App to hold write access to contents and pull requests on this repository.
        """
        write = self._headers(self.installation_token(repo, {"contents": "write", "pull_requests": "write"}).token)
        base_branch, base_sha = self.default_branch_head(repo)
        made = self._send("POST", f"{self._api}/repos/{repo}/git/refs", headers=write,
                          json={"ref": f"refs/heads/{branch}", "sha": base_sha})
        if made.status_code != 201:
            raise GitHubError(f"branch creation failed: {made.status_code}")
        import base64

        put = self._send("PUT", f"{self._api}/repos/{repo}/contents/{path}", headers=write,
                         json={"message": message, "branch": branch,
                               "content": base64.b64encode(content.encode()).decode()})
        if put.status_code not in (200, 201):
            raise GitHubError(f"file creation failed: {put.status_code}")
        pull = self._send("POST", f"{self._api}/repos/{repo}/pulls", headers=write,
                          json={"title": title, "head": branch, "base": base_branch, "body": body})
        if pull.status_code != 201:
            raise GitHubError(f"pull request creation failed: {pull.status_code}")
        info = pull.json()
        # Auto-merge is best effort: it needs the repository setting, and only GraphQL can turn it on.
        self._send("POST", f"{self._api}/graphql", headers=write, json={
            "query": "mutation($id: ID!) { enablePullRequestAutoMerge(input: {pullRequestId: $id, mergeMethod: MERGE})"
                     " { clientMutationId } }",
            "variables": {"id": info.get("node_id")},
        })
        return info["html_url"]
