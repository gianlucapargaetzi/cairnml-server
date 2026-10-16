"""Usage history of the machines and the jobs, in Elasticsearch next to the worker statistics.

One monthly index per company. A machine sample has task "", a job sample has the task id. Recording is
best effort: a failing Elasticsearch never fails a sync, because the sync is what keeps the scheduler running."""

from datetime import datetime
from typing import List, Optional

import elasticsearch.helpers

from apiserver.es_factory import es_factory

NUMERIC = ("cpu_percent", "ram_used_gb", "disk_used_percent", "ram_gb", "peak_ram_gb", "cpu_seconds")


def index_pattern(company: str) -> str:
    return f"scheduler_usage_{company.lower()}_*"


def _index(company: str, when: datetime) -> str:
    return f"scheduler_usage_{company.lower()}_{when:%Y-%m}"


def _samples(company, worker, machine, jobs, now):
    ts = int(now.timestamp() * 1000)
    index = _index(company, now)
    if machine:
        source = {"timestamp": ts, "worker": worker, "task": "", "kind": "machine"}
        source.update({k: float(machine[k]) for k in NUMERIC if machine.get(k) is not None})
        yield {"_index": index, "_source": source}
    for job in jobs or []:
        usage = job.get("usage") or {}
        if not usage:
            continue
        source = {"timestamp": ts, "worker": worker, "task": job["task"], "kind": "job"}
        source.update({k: float(usage[k]) for k in NUMERIC if usage.get(k) is not None})
        yield {"_index": index, "_source": source}


def record(company: str, worker: str, machine: Optional[dict], jobs: List[dict], now: datetime,
           client=None) -> int:
    actions = list(_samples(company, worker, machine, jobs, now))
    if not actions:
        return 0
    try:
        added, _ = elasticsearch.helpers.bulk(client or es_factory.connect("workers"), actions, refresh=True)
        return added
    except Exception:
        return 0


def query(company: str, start: datetime, end: datetime, worker: Optional[str] = None,
          task: Optional[str] = None, client=None, size: int = 1000) -> List[dict]:
    must = [{"range": {"timestamp": {"gte": int(start.timestamp() * 1000), "lte": int(end.timestamp() * 1000)}}}]
    if worker:
        must.append({"term": {"worker.keyword": worker}})
    if task:
        must.append({"term": {"task.keyword": task}})
    res = (client or es_factory.connect("workers")).search(
        index=index_pattern(company),
        body={"size": size, "sort": [{"timestamp": "asc"}], "query": {"bool": {"must": must}}},
        ignore_unavailable=True,
    )
    return [hit["_source"] for hit in res["hits"]["hits"]]


def delete_worker(company: str, worker: str, client=None) -> None:
    (client or es_factory.connect("workers")).delete_by_query(
        index=index_pattern(company),
        body={"query": {"term": {"worker.keyword": worker}}},
        ignore_unavailable=True,
        refresh=True,
    )
