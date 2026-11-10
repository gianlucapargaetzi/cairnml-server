"""
Live progress of a running task.

The SDK reports how far a run is (the outermost progress bar of the script, and the bars inside it) every few
seconds. That is too often for the database, so the latest value lives in Redis and expires on its own. The
database only gets a coarse value (runtime.progress, runtime.progress_label), so that lists, exports and a run
that has ended still have one.
"""
import json
import time
from typing import Any, Dict, Mapping, Optional, Sequence

from apiserver.apierrors import errors

PERSIST_STEP = 10  # the percentage is stored in the database when it moved by at least this much
EXPIRES_SECONDS = 900  # a run that stopped reporting disappears from the live values after this long
MAX_BARS = 4  # outermost bar and the bars inside it
MAX_LABEL = 100


def _key(company_id: str, task_id: str) -> str:
    return f"task_progress_{company_id}_{task_id}"


def clean_bars(bars: Optional[Sequence[Mapping[str, Any]]]) -> Sequence[Dict[str, Any]]:
    """The bars as sent by a client, checked and cut to what is shown: label, count and total of each level"""
    cleaned = []
    for bar in (bars or [])[:MAX_BARS]:
        if not isinstance(bar, Mapping):
            raise errors.bad_request.ValidationError("progress bars must be objects", field="bars")
        label = str(bar.get("label") or "")[:MAX_LABEL]
        n, total = bar.get("n"), bar.get("total")
        for name, value in (("n", n), ("total", total)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0):
                raise errors.bad_request.ValidationError(f"progress bar {name} must be a number >= 0", field="bars")
        cleaned.append(
            {
                "label": label,
                "n": None if n is None else int(n),
                "total": None if total is None else int(total),
            }
        )
    return cleaned


class TaskProgress:
    def __init__(self, redis):
        self.redis = redis

    def update(
        self,
        company_id: str,
        task_id: str,
        percent: Optional[int],
        bars: Optional[Sequence[Mapping[str, Any]]],
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Remember the latest progress of the task. Returns what has to be written to the database, or None.
        No bars: the run is idle, shown as an animation. The percentage is None while no bar has a known total: then the last known one stays.
        """
        if percent is not None and not 0 <= percent <= 100:
            raise errors.bad_request.ValidationError("percent must be between 0 and 100", field="percent")
        bars = clean_bars(bars)

        key = _key(company_id, task_id)
        previous = self._load(self.redis.get(key))
        if not bars:
            percent = None  # idle: no bar that counts is running (loading, saving, a loop that is not the pinned one)
        elif percent is None:
            percent = previous.get("percent")  # no bar has a known total (yet): the last percentage stays
        persisted = previous.get("persisted")

        label = bars[0]["label"] if bars else ""
        persist = bars and percent is not None and (
            persisted is None
            or abs(percent - persisted) >= PERSIST_STEP
            or percent == 100
            or label != previous.get("persisted_label", label)
        )
        value = {
            "percent": percent,
            "bars": bars,
            "updated": now if now is not None else time.time(),
            "persisted": percent if persist else persisted,
            "persisted_label": label if persist else previous.get("persisted_label", label),
        }
        self.redis.set(key, json.dumps(value), ex=EXPIRES_SECONDS)

        if not persist:
            return None
        return {"progress": str(percent), "progress_label": label}

    def get(self, company_id: str, task_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """The live progress of the given tasks; tasks that are not reporting are left out"""
        task_ids = list(dict.fromkeys(task_ids))
        if not task_ids:
            return {}
        values = self.redis.mget([_key(company_id, task_id) for task_id in task_ids])
        result = {}
        for task_id, raw in zip(task_ids, values):
            value = self._load(raw)
            if value:
                result[task_id] = {k: value.get(k) for k in ("percent", "bars", "updated")}
        return result

    @staticmethod
    def _load(raw) -> Dict[str, Any]:
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}
