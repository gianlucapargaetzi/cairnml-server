"""The file server checks a caller's own token with the API server, and holds no credential of its own.

Run from the fileserver folder: python -m unittest discover -s tests
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import TestCase

from werkzeug.exceptions import HTTPException

from auth import TokenInfo, fetch_token_info


class FakeApiServer(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        FakeApiServer.seen.append((self.path, self.headers.get("Authorization")))
        if self.headers.get("Authorization") == "Bearer good-token":
            body = {"meta": {"result_code": 200}, "data": {"user": {"id": "u1", "company": {"id": "c1"}}}}
            status = 200
        else:
            body = {"meta": {"result_code": 401}, "data": {}}
            status = 401
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *args):
        pass


class TestTokenCheck(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeApiServer)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeApiServer.seen.clear()

    def test_a_valid_token_gives_its_user_and_company_and_is_sent_as_its_own_bearer(self):
        info = fetch_token_info(self.url, "good-token")
        self.assertEqual(info, TokenInfo(company="c1", user="u1"))
        path, auth = FakeApiServer.seen[0]
        self.assertTrue(path.endswith("/v2.8/users.get_current_user"))
        self.assertEqual(auth, "Bearer good-token")

    def test_a_refused_token_is_refused(self):
        with self.assertRaises(HTTPException) as ctx:
            fetch_token_info(self.url, "bad-token")
        self.assertEqual(ctx.exception.code, 401)

    def test_an_unreachable_api_server_is_an_internal_error(self):
        with self.assertRaises(HTTPException) as ctx:
            fetch_token_info("http://127.0.0.1:1", "good-token")
        self.assertEqual(ctx.exception.code, 500)
