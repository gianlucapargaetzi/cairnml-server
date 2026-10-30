from unittest import TestCase, mock

from flask import Flask
from flask_cors import CORS

from apiserver.server_init import cors


class FakeConfig:
    def __init__(self, cors_config, web_url=""):
        self.values = {"apiserver.cors": cors_config, "apiserver.idp.web_url": web_url}

    def get(self, key, default=None):
        return self.values.get(key, default)


def app_with(cors_config, web_url=""):
    app = Flask(__name__)

    @app.route("/debug.ping")
    def ping():
        return "pong"

    with mock.patch.object(cors, "config", FakeConfig(cors_config, web_url)):
        CORS(app, **cors.cors_settings())
    return app


def origin_answer(app, origin):
    return app.test_client().get("/debug.ping", headers={"Origin": origin}).headers


class TestCorsPolicy(TestCase):
    def test_a_foreign_origin_is_not_reflected_by_default(self):
        headers = origin_answer(app_with({"origins": [], "supports_credentials": False}), "http://evil.example")

        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertNotIn("Access-Control-Allow-Credentials", headers)

    def test_only_listed_origins_are_allowed(self):
        app = app_with({"origins": ["https://app.example"], "supports_credentials": True})

        allowed = origin_answer(app, "https://app.example")
        refused = origin_answer(app, "http://evil.example")

        self.assertEqual(allowed["Access-Control-Allow-Origin"], "https://app.example")
        self.assertEqual(allowed["Access-Control-Allow-Credentials"], "true")
        self.assertNotIn("Access-Control-Allow-Origin", refused)

    def test_the_configured_web_address_is_allowed(self):
        app = app_with({"origins": [], "supports_credentials": False}, web_url="https://cairnml.example/cairnml/")

        headers = origin_answer(app, "https://cairnml.example")

        self.assertEqual(headers["Access-Control-Allow-Origin"], "https://cairnml.example")

    def test_a_wildcard_with_credentials_refuses_to_start(self):
        with self.assertRaises(RuntimeError):
            with mock.patch.object(cors, "config", FakeConfig({"origins": "*", "supports_credentials": True})):
                cors.cors_settings()

    def test_a_wildcard_without_credentials_is_left_to_the_operator(self):
        with mock.patch.object(cors, "config", FakeConfig({"origins": "*", "supports_credentials": False})):
            settings = cors.cors_settings()

        self.assertEqual(settings["origins"], ["*"])
