# Sign-in

The web app holds no credential of its own. The browser is sent to the identity provider (Keycloak, realm `cairnml`). When the sign-in comes back, the server sets an HttpOnly session cookie. A second cookie holds the ID token and is sent only to the logout endpoint.

## Sign-in with PKCE

```mermaid
sequenceDiagram
    participant WebUI
    participant Server
    participant IDP

    WebUI->>Server: GET login.supported_modes
    Server-->>WebUI: basic login disabled, not signed in
    Note over WebUI: no session, go to login.idp_start
    WebUI->>Server: GET login.idp_start
    Note over Server: create state, nonce and PKCE verifier, keep them in Redis for 600 s
    Server-->>WebUI: 302 to IDP authorize (client cairnml-web, PKCE, state)
    WebUI->>IDP: sign in with the user's credentials
    IDP-->>WebUI: 302 login.idp_callback?code&state
    WebUI->>Server: GET login.idp_callback?code&state
    Note over Server: take the stored login for this state, once, refuse if unknown
    Server->>IDP: POST /token (code, PKCE verifier)
    IDP-->>Server: id_token, access_token
    Note over Server: check nonce, verify access token (azp cairnml-web, RS256 via JWKS)
    Note over Server: upsert user, admin role if realm role cairnml-admin, audit web.login
    Note over Server: issue web token (kind web, 15 minutes)
    Server-->>WebUI: 302 to web app, Set-Cookie session and id-token cookie
```

## Authenticated request

```mermaid
sequenceDiagram
    participant WebUI
    participant Server

    Note over Server: CSRF check, a state-changing request must come from the web origin
    Note over Server: verify signature, expiry and revocation (Redis), user must be enabled
    alt token valid
        WebUI->>Server: POST service.action (session cookie, Origin)
        Note over Server: run the service, read or write MongoDB
        Server-->>WebUI: 200 with the result
    else token expired or revoked
        WebUI->>Server: POST service.action (session cookie, Origin)
        Server-->>WebUI: 401, the web app signs in again
    end
```

## Logout

```mermaid
sequenceDiagram
    participant WebUI
    participant Server
    participant IDP

    WebUI->>Server: POST login.logout (session and id-token cookies)
    Note over Server: revoke the session id in Redis, clear both cookies
    Server-->>WebUI: redirect_url, Keycloak end-session with id_token_hint
    WebUI->>IDP: GET end-session
    IDP-->>WebUI: redirect to login.idp_start
```

Without the id-token hint Keycloak asks the person to confirm the logout, so the cookie is kept for that call.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `login.supported_modes` | Tells the web app that only the identity provider is used |
| `login.idp_start` | Starts a sign-in: creates state and PKCE values and redirects to Keycloak |
| `login.idp_callback` | Completes the sign-in: exchanges the code, checks the token, sets the cookies |
| `login.logout` | Ends the CairnML session and returns the Keycloak end-session URL |

## Cookies

| Cookie | Attributes | Purpose |
|---|---|---|
| `cairnml_token_basic` | HttpOnly, SameSite=Strict, path `/cairnml` | The web session token |
| `cairnml_idp_session` | HttpOnly, SameSite=Strict, path `/cairnml/api/v999.0/login.logout` | The ID token, sent only to logout |
