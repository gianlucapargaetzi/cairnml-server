from .session import Session


def run(session):
    return session.of("tasks.get_all"), Session
