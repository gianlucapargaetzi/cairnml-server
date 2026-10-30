// 1) Enrollment: a machine signs in once and registers its own public key.
#import "_chronos.typ": *


#block(breakable: false)[
= Enrollment
== SDK enrollment (`cairnml init <server>`)

#seq-diagram(("SDK", "Server", "IDP"), (
  call("SDK", "Server", "GET devices.idp_settings"),
  reply("Server", "SDK", "issuer, client id, public_url"),
  process("SDK", "Generate Ed25519 key pair; the private key stays in ~/.cairnml/device.key (0600)"),
  call("SDK", "IDP", "browser: sign in with PKCE"),
  reply("IDP", "SDK", "authorization code (127.0.0.1:8765)"),
  call("SDK", "IDP", "POST /token (code, verifier)"),
  reply("IDP", "SDK", "id_token, access_token"),
  call("SDK", "Server", "POST devices.enroll (access_token, public key)"),
  process("Server", "Verify token (client cairnml-sdk, JWKS signature); upsert user; store Device with the public key; audit device.enrolled"),
  reply("Server", "SDK", "{id: device_id}"),
  process("SDK", "Store the device id; later sessions sign assertions with the key"),
))
]

#block(breakable: false)[
== Agent enrollment (`cairnml-agent init <server>`)

The same handshake with the agent's own key and kind `agent`. The server refuses unless the signed-in user is an admin.

#seq-diagram(("Agent", "Server", "IDP"), (
  call("Agent", "Server", "GET devices.idp_settings"),
  reply("Server", "Agent", "issuer, client id, public_url"),
  process("Agent", "Generate agent Ed25519 key; stored in ~/.cairnml/agent/device.key (0600)"),
  call("Agent", "IDP", "browser: admin signs in with PKCE"),
  reply("IDP", "Agent", "authorization code"),
  call("Agent", "IDP", "POST /token"),
  reply("IDP", "Agent", "tokens"),
  alt((
    (guard: "user is admin", steps: (
      call("Agent", "Server", "POST devices.enroll (kind=agent, public key)"),
      process("Server", "Store Device(kind=agent) owned by the admin; audit device.enrolled"),
      reply("Server", "Agent", "{id: device_id}"),
      process("Agent", "Store the device id"),
    )),
    (guard: "user is not admin", steps: (
      call("Agent", "Server", "POST devices.enroll (kind=agent, public key)"),
      process("Server", "Refuse; audit device.enrol_refused; nothing stored"),
      reply("Server", "Agent", "error: only an admin may enrol an agent"),
    )),
  )),
))
]

#block(breakable: false)[
== Device token exchange (every SDK or agent session)

The device proves possession of its key with a short signed assertion, which can be used once. The server returns a 15-minute access token that carries the owner's role.

#seq-diagram(("Agent", "Server"), (
  process("Agent", "Sign assertion: iss = device id, aud = public_url, jti random, lifetime at most 300 s"),
  process("Server", "Find the Device from iss; refuse if revoked or its owner is disabled; verify signature, audience and times; claim jti once"),
  alt((
    (guard: "all checks pass", steps: (
      call("Agent", "Server", "POST devices.exchange (assertion)"),
      process("Server", "Issue access token: kind = agent, role = owner's role, 900 s"),
      reply("Server", "Agent", "{access_token, expires_in}"),
    )),
    (guard: "any check fails", steps: (
      call("Agent", "Server", "POST devices.exchange (assertion)"),
      process("Server", "Audit token.refused with the reason"),
      reply("Server", "Agent", "401 invalid token"),
    )),
  )),
))
]
