"""Writes a throwaway identity-provider key pair for the end-to-end tests.

Run once: python -m apiserver.tests.idp.make_test_idp <dir>
The files are test-only and live outside the repository, in the workspace.
"""

import json
import os
import sys

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

KID = "test-key"


def main(directory: str) -> None:
    os.makedirs(directory, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid=KID, alg="RS256", use="sig")
    with open(os.path.join(directory, "idp_private.pem"), "wb") as handle:
        handle.write(pem)
    with open(os.path.join(directory, "jwks.json"), "w", encoding="utf-8") as handle:
        json.dump({"keys": [jwk]}, handle)
    print("written to", directory)


if __name__ == "__main__":
    main(sys.argv[1])
