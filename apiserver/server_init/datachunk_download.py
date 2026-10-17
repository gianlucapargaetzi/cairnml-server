"""Browser downloads of stored data: GET /datachunks/<kind>/<task>/<name> redirects to a presigned URL.

Browsers cannot send a bearer token with <img> tags or plain links, so this route also accepts the
session cookie. The redirect is only issued for a stored object of the caller's company.
"""

from flask import Flask, Response, redirect, request

from apiserver.apierrors import errors
from apiserver.bll import datachunks as bll
from apiserver.config_repo import config
from apiserver.database.model.datachunk import Datachunk, DatachunkState
from apiserver.service_repo.auth import Token
from apiserver.service_repo.auth.auth import check_idp_token, is_token_revoked

ROUTE = "/datachunks/<kind>/<task_id>/<path:name>"


def _encoded_token() -> str:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.cookies.get(config.get("apiserver.auth.session_auth_cookie_name"), "")


def _company() -> str:
    encoded = _encoded_token()
    if not encoded:
        raise errors.unauthorized.NoCredentials()
    try:
        token = Token.from_encoded_token(encoded)
    except Exception:
        raise errors.unauthorized.InvalidToken("invalid token") from None
    if is_token_revoked(token):
        raise errors.unauthorized.InvalidToken("revoked token")
    check_idp_token(encoded)
    return token.identity.company


def download(kind: str, task_id: str, name: str) -> Response:
    try:
        company = _company()
    except errors.BaseError as error:
        return Response(error.msg, status=error.code, mimetype="text/plain")

    chunk = Datachunk.objects(
        company=company, task=task_id, kind=kind, name=name, state=DatachunkState.stored
    ).order_by("-stored").first()
    if not chunk:
        return Response("not found", status=404, mimetype="text/plain")

    url = bll.prepare_download(company=company, chunk=chunk)["url"]
    response = redirect(url, code=302)
    response.headers["Cache-Control"] = "no-store"
    return response


def register(app: Flask) -> None:
    app.add_url_rule(ROUTE, endpoint="datachunk_download", view_func=download, methods=["GET"])
