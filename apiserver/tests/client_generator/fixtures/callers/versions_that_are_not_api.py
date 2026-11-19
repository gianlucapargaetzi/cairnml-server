import requests

__version__ = "0.0.6"


def check(session):
    session.check_min_server_version("3.26")
    return requests.get("https://example.invalid/api/v2"), dict(package="1.2.3")
