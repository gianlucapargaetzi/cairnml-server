"""A signing key the IdP removes stops verifying tokens once the key set expires. Runs anywhere."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import TestCase

from apiserver.idp.oidc import OidcError, OidcVerifier
from apiserver.tests.idp.idp_stub import FakeIdp


class TestKeyLifespan(TestCase):
    def setUp(self):
        self.idp = FakeIdp()
        self.served = json.dumps(self.idp.jwks()).encode()
        served = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(served.served)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        url = f"http://127.0.0.1:{self.server.server_address[1]}/certs"
        self.verifier = OidcVerifier(FakeIdp.ISSUER, "cairnml-sdk", jwks_url=url, key_lifespan_sec=1)

    def test_a_removed_key_is_refused_once_the_key_set_expires(self):
        token = self.idp.token()
        self.verifier.verify(token)

        self.served = json.dumps({"keys": []}).encode()
        time.sleep(1.5)

        with self.assertRaises(OidcError):
            self.verifier.verify(token)
