def run(session):
    session.send_request(service="demo", action="nope")
    session.send_request(service="demo", action="get_all", version="2.5")
