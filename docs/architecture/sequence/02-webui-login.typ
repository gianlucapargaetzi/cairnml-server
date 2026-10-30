// 2) WebUI login, session cookies and logout.
#import "_chronos.typ": *


The web app holds no credential of its own. The browser goes to the identity provider; the server then sets an HttpOnly session cookie and a second cookie with the ID token, which is sent only to the logout endpoint.

#block(breakable: false)[
= WebUI login and session
== Sign-in (authorization code with PKCE)

#seq-diagram(("WebUI", "Server", "IDP"), (
  call("WebUI", "Server", "GET login.supported_modes"),
  reply("Server", "WebUI", "basic login disabled; not signed in"),
  process("WebUI", "No session: navigate to login.idp_start"),
  call("WebUI", "Server", "GET login.idp_start"),
  process("Server", "Create state, nonce and PKCE verifier; keep them in Redis for 600 s"),
  reply("Server", "WebUI", "302 to IDP authorize (cairnml-web, PKCE, state)"),
  call("WebUI", "IDP", "sign in with the user's credentials"),
  reply("IDP", "WebUI", "302 login.idp_callback?code&state"),
  call("WebUI", "Server", "GET login.idp_callback?code&state"),
  process("Server", "Take the stored login for this state, once; refuse if unknown"),
  call("Server", "IDP", "POST /token (code, PKCE verifier)"),
  reply("IDP", "Server", "id_token, access_token"),
  process("Server", "Check nonce; verify access token (azp cairnml-web, RS256 via JWKS); upsert user; admin role if realm role cairnml-admin; audit web.login"),
  process("Server", "Issue web token: kind web, 900 s"),
  reply("Server", "WebUI", "302 to web app; Set-Cookie session (HttpOnly, SameSite=Strict, /cairnml) and id-token cookie (logout path only)"),
))
]

#block(breakable: false)[
== Authenticated request

#seq-diagram(("WebUI", "Server"), (
  process("Server", "CSRF check: a state-changing request must come from the web origin"),
  process("Server", "Verify token signature, expiry and revocation (Redis); user must exist and be enabled"),
  alt((
    (guard: "token valid", steps: (
      call("WebUI", "Server", "POST <service>.<action> (session cookie, Origin)"),
      process("Server", "Run the service; read or write MongoDB"),
      reply("Server", "WebUI", "200 with the result"),
    )),
    (guard: "token expired or revoked", steps: (
      call("WebUI", "Server", "POST <service>.<action> (session cookie, Origin)"),
      reply("Server", "WebUI", "401: the web app signs in again"),
    )),
  )),
))
]

#block(breakable: false)[
== Logout

#seq-diagram(("WebUI", "Server", "IDP"), (
  call("WebUI", "Server", "POST login.logout (session and id-token cookies)"),
  process("Server", "Revoke the token's session id in Redis; clear both cookies"),
  reply("Server", "WebUI", "redirect_url: IDP end-session with id_token_hint"),
  call("WebUI", "IDP", "GET end-session (ends the Keycloak session)"),
  reply("IDP", "WebUI", "redirect to login.idp_start"),
))
]
