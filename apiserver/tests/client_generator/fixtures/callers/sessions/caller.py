import requests
import boto3

from .session import Session as _Session


class Task:
    def __init__(self, session):
        self._session = session
        self.task_session = session

    def run(self):
        self._session.send_request(service="demo", action="get_all")
        self._session.get_with_act_as("z")
        self.task_session.refresh_token()
        self.task_session.feature_set
        self._session.config.get("agent")
        self._session.finalize_config(self._session.config)


def plain_http(url):
    session = requests.Session()
    session.mount("https://", None)
    return session.get(url)


def boto(region):
    session = boto3.Session(region_name=region)
    return session.resource("s3")


def typed(session: _Session):
    session.finalize_config({})
    return session.send_request(service="demo", action="get_all")
