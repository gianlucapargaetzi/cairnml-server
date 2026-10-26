"""Creates and removes the test accounts in the cairnml realm, through Keycloak's admin REST API."""

import requests

from . import settings


class Keycloak:
    def __init__(self, http=None):
        self.http = http or requests.Session()
        self._token = None

    def _admin_headers(self):
        if self._token is None:
            reply = self.http.post(
                f"{settings.IDP_URL}/realms/master/protocol/openid-connect/token",
                data={
                    "grant_type": "password",
                    "client_id": "admin-cli",
                    "username": settings.ADMIN_USER,
                    "password": settings.ADMIN_PASSWORD,
                },
                timeout=30,
            )
            reply.raise_for_status()
            self._token = reply.json()["access_token"]
        return {"Authorization": f"Bearer {self._token}"}

    def _admin(self, method, path, **kwargs):
        """Calls the admin API. The admin token is short-lived, so an expired one is replaced and the call retried."""
        url = f"{settings.IDP_URL}/admin/realms/{settings.REALM}{path}"
        reply = self.http.request(method, url, headers=self._admin_headers(), timeout=30, **kwargs)
        if reply.status_code == 401:
            self._token = None
            reply = self.http.request(method, url, headers=self._admin_headers(), timeout=30, **kwargs)
        return reply

    def user_id(self, username):
        found = self._admin("GET", "/users", params={"username": username, "exact": "true"}).json()
        return found[0]["id"] if found else None

    def create_user(self, username, password, *, admin=False):
        """Creates an enabled user with a password that does not expire; admin adds the cairnml-admin role."""
        self.delete_user(username)
        body = {
            "username": username,
            "enabled": True,
            "emailVerified": True,
            "email": f"{username}@example.test",
            "firstName": "E2E",
            "lastName": username,
            "credentials": [{"type": "password", "value": password, "temporary": False}],
        }
        reply = self._admin("POST", "/users", json=body)
        reply.raise_for_status()
        user_id = self.user_id(username)
        if admin:
            role = self._admin("GET", "/roles/cairnml-admin").json()
            self._admin("POST", f"/users/{user_id}/role-mappings/realm", json=[role]).raise_for_status()
        return user_id

    def delete_user(self, username):
        user_id = self.user_id(username)
        if user_id:
            self._admin("DELETE", f"/users/{user_id}").raise_for_status()

    def set_enabled(self, username, enabled: bool):
        """Enables or disables an account. The representation is read first, so only 'enabled' changes."""
        user_id = self.user_id(username)
        representation = self._admin("GET", f"/users/{user_id}").json()
        representation["enabled"] = enabled
        self._admin("PUT", f"/users/{user_id}", json=representation).raise_for_status()
