"""Usage of the machines and the jobs, for the monitor. It is recorded from the workers' status reports."""

from apiserver.service_repo import APICall, endpoint


@endpoint("scheduler.get_usage_history", min_version="2.36")
def get_usage_history(call: APICall, company_id, _):
    from datetime import datetime, timedelta
    from apiserver.bll.scheduler import usage_history

    body = call.data or {}
    minutes = min(int(body.get("minutes") or 60), 24 * 60)
    now = datetime.utcnow()
    points = usage_history.query(company_id, start=now - timedelta(minutes=minutes), end=now,
                                 worker=body.get("worker"), task=body.get("task"))
    call.result.data = {"points": points}
