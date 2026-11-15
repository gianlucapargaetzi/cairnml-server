from backend_api.services import demo as d, other


def run(session):
    d.GetAllRequest(name="n", page=1)
    d.GetByIdRequest(queue="q")
    other.Anything(whatever=1)
    session.send_request(service="demo", action="get_all", version="2.15")
    session.send_request(service="demo", action="get_all", version="2.30")
