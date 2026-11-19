from abc import ABC


class TokenManager(ABC):
    def refresh_token(self):
        return None


class Session(TokenManager):
    feature_set = "basic"

    def __init__(self):
        self.config = {}
        self._callback = None

    def send_request(self, service, action, **kwargs):
        return None

    def get_with_act_as(self, endpoint):
        return None

    @property
    def server(self):
        return ""
