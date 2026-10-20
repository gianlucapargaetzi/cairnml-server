"""The scheduler's timer: finds lost workers and expired offers every sweep interval, whether or not a placement pass
runs. Without it a stopped worker is marked lost only when some task is placed (see ``service.sweep``)."""

import logging
import threading

from apiserver.bll.scheduler import service
from apiserver.config_repo import config
from apiserver.database.model.scheduler import SchedulerWorker

log = logging.getLogger("clearml.scheduler.sweep")


def sweep_once() -> int:
    """One pass over every company that has workers. Returns the number of workers marked lost."""
    lost = 0
    for company in SchedulerWorker.objects().distinct("company"):
        try:
            lost += len(service.sweep(company))
        except Exception:  # one company's failure must not stop the others, or the timer
            log.exception("scheduler sweep failed for company %s", company)
    return lost


class SweepThread(threading.Thread):
    def __init__(self, interval: float) -> None:
        super().__init__(name="scheduler-sweep", daemon=True)
        self._interval = interval
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self._interval):
            try:
                lost = sweep_once()
                if lost:
                    log.info("scheduler sweep marked %d worker(s) lost", lost)
            except Exception:  # keep the timer alive; the next pass retries
                log.exception("scheduler sweep pass failed")

    def stop(self) -> None:
        self._stop_event.set()


def start_from_config() -> None:
    interval = float(config.get("services.scheduler.sweep_interval_sec", 15))
    if interval <= 0:
        return
    SweepThread(interval).start()
