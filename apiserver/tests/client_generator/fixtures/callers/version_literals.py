def enrol(server):
    return server.rstrip("/") + "/v2.8/"


def dataset(session, version: str = "2.40"):
    return session.send_request(service="demo", action="get_all", version="2.15"), version
