"""
Generates the Python clients (SDK, agent) from the API schema in apiserver/schema/services.

Run `python -m apiserver.client_generator --help`. The generator needs only pyhocon and jinja2 (not the server's
configuration), so a client repository can run it from a checkout of this package.
"""
