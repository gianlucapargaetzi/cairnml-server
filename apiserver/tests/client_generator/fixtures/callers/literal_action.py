class Session:
    def send_request(self, service, action, **kwargs):
        pass

    def get(self, service, action, **kwargs):
        pass

    def post(self, service, action, **kwargs):
        pass

    def send(self, req_obj, headers=None):
        pass


def run(session):
    session.send_request(service="demo", action="get_all", json={})
