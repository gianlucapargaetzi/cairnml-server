# Token exchange

Every SDK or agent session turns its device key into a short-lived access token. The device proves that it holds the key by signing a short assertion. The server checks the assertion and issues a token that carries the owner's role. Each assertion can be used once.

```mermaid
sequenceDiagram
    participant Agent
    participant Server

    Note over Agent: sign assertion, iss = device id, aud = public_url, jti random, lifetime at most 300 s
    Agent->>Server: POST devices.exchange (assertion, no auth)
    Note over Server: find the Device from iss, refuse if revoked or its owner is disabled
    Note over Server: verify signature, audience and times (30 s leeway), claim jti in Redis once
    alt all checks pass
        Note over Server: issue access token, kind agent or client, role of the owner, 15 minutes
        Server-->>Agent: access_token, expires_in
    else any check fails
        Note over Server: audit token.refused with the reason
        Server-->>Agent: 401 invalid token
    end
```

## Why a signed assertion

The device key is never sent over the network. An assertion is bound to one server address (`aud`), so a copy captured on the way to another server is useless there. The `jti` value is stored in Redis for the assertion's lifetime, so a replayed assertion is refused.

## Endpoint

| Endpoint | Auth | Purpose |
|---|---|---|
| `devices.exchange` | assertion only | Issues an access token for a live device |

## What the token carries

| Claim | Value |
|---|---|
| `kind` | `client` (SDK) or `agent` |
| `device` | the device id |
| `identity` | the owner's user id, company and role |
| `exp` | 15 minutes after issue |

Revoking a device or disabling its owner stops new exchanges at once. A token already issued stays valid until it expires, at most 15 minutes.
