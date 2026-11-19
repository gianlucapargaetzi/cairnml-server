from backend_api.services import demo


class Session:
    def send(self, req_obj, headers=None):
        pass


def run(session: Session):
    session.send(demo.GetAllRequest(name="n"))
