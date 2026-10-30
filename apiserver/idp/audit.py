"""Audit events for sign-ins, enrolments, approvals, tokens, storage and GitHub access."""

from typing import Optional

from apiserver.database.model.audit import AuditEvent

IP_MAX_LENGTH = 64


def client_address(call) -> str:
    """The address the client used. The proxy sets X-Real-IP from the connection, so a client cannot choose it;
    X-Forwarded-For, which a client can send, is used only when the proxy did not set X-Real-IP."""
    return (call.get_header(call.HEADER_REAL_IP) or call.real_ip)[:IP_MAX_LENGTH]


def build_event(actor: str, action: str, ip: Optional[str] = None, company: Optional[str] = None, **detail) -> AuditEvent:
    return AuditEvent(actor=actor, action=action, ip=ip, company=company, detail=detail)


def record(actor: str, action: str, ip: Optional[str] = None, company: Optional[str] = None, **detail) -> None:
    build_event(actor, action, ip=ip, company=company, **detail).save()
