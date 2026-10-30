# Device enrolment

A machine signs in once, generates its own Ed25519 key, and registers the public key with the server. The private key never leaves the machine. The SDK (`cairnml init`) and the agent (`cairnml-agent init`) use the same handshake. They differ in the key they create and in who may enrol them.

## SDK machine

```mermaid
sequenceDiagram
    participant SDK
    participant Server
    participant IDP

    SDK->>Server: GET devices.idp_settings (no auth)
    Server-->>SDK: issuer, client id, public_url
    Note over SDK: generate Ed25519 key, private key stays in ~/.cairnml/device.key (0600)
    SDK->>IDP: browser sign-in with PKCE (listener on 127.0.0.1:8765)
    IDP-->>SDK: authorization code
    SDK->>IDP: POST /token (code, verifier)
    IDP-->>SDK: id_token, access_token
    SDK->>Server: POST devices.enroll (access_token, public key, kind client)
    Note over Server: verify token (client cairnml-sdk, JWKS signature), upsert user
    Note over Server: store Device with the public key, audit device.enrolled
    Server-->>SDK: device id
    Note over SDK: store the device id, later sessions sign assertions with the key
```

## Agent

The agent's key is stored in `~/.cairnml/agent/device.key`. The server accepts the enrolment only when the signed-in person is an admin, because an agent runs other people's jobs.

```mermaid
sequenceDiagram
    participant Agent
    participant Server
    participant IDP

    Agent->>Server: GET devices.idp_settings
    Server-->>Agent: issuer, client id, public_url
    Note over Agent: generate agent key, stored in ~/.cairnml/agent/device.key (0600)
    Agent->>IDP: browser sign-in as an admin (PKCE)
    IDP-->>Agent: authorization code
    Agent->>IDP: POST /token
    IDP-->>Agent: tokens
    alt user is admin
        Agent->>Server: POST devices.enroll (kind agent, public key)
        Note over Server: store Device with kind agent, owned by the admin, audit device.enrolled
        Server-->>Agent: device id
    else user is not admin
        Agent->>Server: POST devices.enroll (kind agent, public key)
        Note over Server: refuse, audit device.enrol_refused, store nothing
        Server-->>Agent: error, only an admin may enrol an agent
    end
```

## Endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| `devices.idp_settings` | none | Returns the issuer and client id the machine needs for sign-in, and the address that device assertions must name |
| `devices.enroll` | identity-provider token | Stores the device's public key and owner. Agents need an admin |

## Notes

- The sign-in listener is fixed at `127.0.0.1:8765`, because the identity provider registers exactly that redirect address. On a machine without a browser this flow does not work yet (see the open issues on headless enrolment).
- `--force` revokes the previous device before enrolling again.
- The owner of a device stays the person who enrolled it. Their disabling stops every device they own (see [Trust model](trust-model.md)).
