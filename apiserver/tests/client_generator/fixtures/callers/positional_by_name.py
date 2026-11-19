def run(session, options):
    session.send_request("demo", "nope")
    session.send_request("routers", "get_routes")
    options.get("routers", "get_routes")
