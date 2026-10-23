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

    def installation_token(self, repo: str) -> InstallationToken:
        """A short-lived token that can read this one repository's contents and nothing else."""
        if not valid_repo(repo):
            raise GitHubError("repo must be owner/name")
        cached = self._tokens.get(repo)
        if cached and cached.expires_at.timestamp() - self._clock() > _REFRESH_MARGIN:
            return cached
        response = self._send(
            "POST", f"{self._api}/app/installations/{self._installation_id(repo)}/access_tokens",
            headers=self._headers(self._app_jwt()),
            json={"repositories": [repo.split("/", 1)[1]], "permissions": {"contents": "read"}},
        )
        if response.status_code != 201:
            raise GitHubError(f"token request failed: {response.status_code}")
        body = response.json()
        try:
            token = InstallationToken(body["token"], datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00")))
        except (KeyError, TypeError, ValueError):
            raise GitHubError("token response is malformed") from None
        self._tokens[repo] = token
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
