"""Sign-in for people in the web app: the identity provider's code flow with PKCE.

The state, nonce and PKCE verifier are created per login and kept in Redis for a short time; the
callback must present the same state, and each state can be used once.
"""

import base64
import hashlib
import json
import secrets
from urllib.parse import urlencode

from apiserver.idp.users import upsert_idp_user

LOGIN_TTL_SEC = 600


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def new_login() -> dict:
    verifier = secrets.token_urlsafe(48)
    return {
        "state": secrets.token_urlsafe(24),
        "nonce": secrets.token_urlsafe(24),
        "verifier": verifier,
    }


def authorize_url(issuer: str, client_id: str, redirect_uri: str, state: str, nonce: str, challenge: str) -> str:
    query = urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return f"{issuer.rstrip('/')}/protocol/openid-connect/auth?{query}"


_KEY = "idp:login:"


def start_login(store, issuer: str, client_id: str, redirect_uri: str) -> str:
    """Create a login, remember its state for LOGIN_TTL_SEC, and return the IdP's sign-in URL."""
    login = new_login()
    store.set(f"{_KEY}{login['state']}", json.dumps({"nonce": login["nonce"], "verifier": login["verifier"]}),
              ex=LOGIN_TTL_SEC)
    return authorize_url(issuer, client_id, redirect_uri, login["state"], login["nonce"],
                         pkce_challenge(login["verifier"]))


class LoginRefused(Exception):
    pass


def finish_login(store, http, verifier, issuer: str, client_id: str, redirect_uri: str,
                 state: str, code: str, admin_role: str):
    """Exchange the code for tokens and create the ClearML user. Returns (user, identity).

    Each state is used once: it is removed from the store before anything else happens.
    """
    raw = store.getdel(f"{_KEY}{state}") if state else None
    if raw is None:
        raise LoginRefused("unknown or already used login")
    pending = json.loads(raw)
    response = http.post(
        f"{issuer.rstrip('/')}/protocol/openid-connect/token",
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
              "client_id": client_id, "code_verifier": pending["verifier"]},
        timeout=10,
    )
    if response.status_code != 200:
        raise LoginRefused("token exchange refused")
    body = response.json()
    try:
        verifier.verify_nonce(body["id_token"], pending["nonce"], clients=[client_id])
        identity = verifier.verify(body["access_token"], clients=[client_id])
    except Exception as exc:  # any token problem gets the same refusal
        raise LoginRefused("invalid identity token") from exc
    user = upsert_idp_user(issuer, identity.subject, identity.username, identity.roles, admin_role)
    if user.idp_disabled:
        raise LoginRefused("user is disabled")
    return user, identity
