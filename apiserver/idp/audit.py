"""Audit events for sign-ins, enrolments, approvals, tokens, storage and GitHub access."""

from typing import Optional

from apiserver.database.model.audit import AuditEvent


def build_event(actor: str, action: str, ip: Optional[str] = None, **detail) -> AuditEvent:
    return AuditEvent(actor=actor, action=action, ip=ip, detail=detail)


def record(actor: str, action: str, ip: Optional[str] = None, **detail) -> None:
    build_event(actor, action, ip=ip, **detail).save()
