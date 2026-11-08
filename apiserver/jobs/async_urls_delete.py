from abc import ABC, ABCMeta, abstractmethod
from argparse import ArgumentParser
from collections import defaultdict
from datetime import datetime, timedelta
from itertools import chain
from pathlib import Path
from time import sleep
from typing import Sequence, Tuple, Mapping, TypeVar, Hashable, Generic

from boltons.iterutils import bucketize, chunked_iter
from mongoengine import Q

from apiserver.bll import datachunks, run_import
from apiserver.config_repo import config
from apiserver.database import db
from apiserver.database.model.datachunk import Datachunk
from apiserver.database.model.url_to_delete import UrlToDelete, StorageType, DeletionStatus

log = config.logger(f"JOB-{Path(__file__).name}")
conf = config.get("services.async_urls_delete")
max_retries = conf.get("max_retries", 3)
retry_timeout = timedelta(seconds=conf.get("retry_timeout_sec", 60))


def mark_retry_failed(ids: Sequence[str], reason: str):
    UrlToDelete.objects(id__in=ids).update(
        last_failure_time=datetime.utcnow(),
        last_failure_reason=reason,
        inc__retry_count=1,
    )
    UrlToDelete.objects(id__in=ids, retry_count__gte=max_retries).update(
        status=DeletionStatus.failed
    )


def mark_failed(query: Q, reason: str):
    UrlToDelete.objects(query).update(
        status=DeletionStatus.failed,
        last_failure_time=datetime.utcnow(),
        last_failure_reason=reason,
    )


T = TypeVar("T", bound=Hashable)


class Storage(Generic[T], metaclass=ABCMeta):
    class Client(ABC):
        @property
        @abstractmethod
        def chunk_size(self) -> int:
            pass

        def get_path(self, url: UrlToDelete) -> str:
            pass

        def delete_many(
            self, paths: Sequence[str]
        ) -> Tuple[Sequence[str], Mapping[str, Sequence[str]]]:
            pass

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    def group_urls(
        self, urls: Sequence[UrlToDelete]
    ) -> Mapping[T, Sequence[UrlToDelete]]:
        pass

    def get_client(self, base: T, urls: Sequence[UrlToDelete]) -> Client:
        pass


def delete_urls(urls_query: Q, storage: Storage):
    to_delete = list(UrlToDelete.objects(urls_query).order_by("url").limit(10000))
    if not to_delete:
        return

    grouped_urls = storage.group_urls(to_delete)
    for base, urls in grouped_urls.items():
        if not base:
            msg = f"Invalid {storage.name} url or missing {storage.name} configuration for account"
            mark_failed(
                Q(id__in=[url.id for url in urls]), msg,
            )
            log.warning(
                f"Failed to delete {len(urls)} files from {storage.name} due to: {msg}"
            )
            continue

        try:
            client = storage.get_client(base, urls)
        except Exception as ex:
            failed = [url.id for url in urls]
            mark_retry_failed(failed, reason=str(ex))
            log.warning(
                f"Failed to delete {len(failed)} files from {storage.name} due to: {str(ex)}"
            )
            continue

        for chunk in chunked_iter(urls, client.chunk_size):
            paths = []
            path_to_id_mapping = defaultdict(list)
            ids_to_delete = set()
            for url in chunk:
                try:
                    path = client.get_path(url)
                except Exception as ex:
                    err = str(ex)
                    mark_failed(Q(id=url.id), err)
                    log.warning(f"Error getting path for {url.url}: {err}")
                    continue

                paths.append(path)
                path_to_id_mapping[path].append(url.id)
                ids_to_delete.add(url.id)

            if not paths:
                continue

            try:
                deleted_paths, errors = client.delete_many(paths)
            except Exception as ex:
                mark_retry_failed([url.id for url in urls], str(ex))
                log.warning(
                    f"Error deleting {len(paths)} files from {storage.name}: {str(ex)}"
                )
                continue

            failed_ids = set()
            for reason, err_paths in errors.items():
                error_ids = set(
                    chain.from_iterable(
                        path_to_id_mapping.get(p, []) for p in err_paths
                    )
                )
                mark_retry_failed(list(error_ids), reason)
                log.warning(
                    f"Failed to delete {len(error_ids)} files from {storage.name} storage due to: {reason}"
                )
                failed_ids.update(error_ids)

            deleted_ids = set(
                chain.from_iterable(
                    path_to_id_mapping.get(p, []) for p in deleted_paths
                )
            )
            if deleted_ids:
                UrlToDelete.objects(id__in=list(deleted_ids)).delete()
                log.info(
                    f"{len(deleted_ids)} files deleted from {storage.name} storage"
                )

            missing_ids = ids_to_delete - deleted_ids - failed_ids
            if missing_ids:
                mark_retry_failed(list(missing_ids), "Not succeeded")


class DatachunkStorage(Storage):
    """Objects stored through datachunk:// locations: removed from the object store, with their records."""

    class Client(Storage.Client):
        def __init__(self, company: str):
            self.company = company

        @property
        def chunk_size(self) -> int:
            return 1000

        def get_path(self, url: UrlToDelete) -> str:
            if not datachunks.is_location(url.url):
                raise ValueError("Not a datachunk location")
            return url.url

        def delete_many(
            self, paths: Sequence[str]
        ) -> Tuple[Sequence[str], Mapping[str, Sequence[str]]]:
            deleted, errors = [], defaultdict(list)
            for path in paths:
                try:
                    kind, task_id, name = datachunks.parse_location(path)
                    chunks = list(
                        Datachunk.objects(company=self.company, task=task_id, kind=kind, name=name)
                    )
                    datachunks.delete(company=self.company, chunks=chunks)
                    deleted.append(path)
                except Exception as ex:
                    errors[str(ex)].append(path)
            return deleted, dict(errors)

    @property
    def name(self) -> str:
        return "Datachunk"

    def __init__(self, company: str):
        self.company = company

    def group_urls(self, urls: Sequence[UrlToDelete]) -> Mapping[str, Sequence[UrlToDelete]]:
        return bucketize(urls, key=lambda u: "datachunk" if datachunks.is_location(u.url) else None)

    def get_client(self, base: str, urls: Sequence[UrlToDelete]) -> Client:
        return self.Client(self.company)


PENDING_UPLOAD_MAX_AGE = timedelta(hours=24)
PURGE_PENDING_EVERY = timedelta(hours=1)


def run_delete_loop():
    storage_helpers = {
        StorageType.datachunk: DatachunkStorage,
    }
    next_purge = datetime.utcnow()
    while True:
        now = datetime.utcnow()
        if now >= next_purge:
            removed = datachunks.purge_stale_pending(PENDING_UPLOAD_MAX_AGE)
            if removed:
                log.info(f"Removed {removed} uploads that were never committed")
            imported_later = run_import.purge_unimported()
            if imported_later:
                log.info(f"Removed {imported_later} staged run uploads that were never imported")
            next_purge = now + PURGE_PENDING_EVERY
        urls_query = (
            Q(status__ne=DeletionStatus.failed)
            & Q(retry_count__lt=max_retries)
            & (
                Q(last_failure_time__exists=False)
                | Q(last_failure_time__lt=now - retry_timeout)
            )
        )

        url_to_delete: UrlToDelete = UrlToDelete.objects(
            urls_query & Q(storage_type__in=list(storage_helpers))
        ).order_by("retry_count").limit(1).first()
        if not url_to_delete:
            sleep(10)
            continue

        company = url_to_delete.company
        user = url_to_delete.user
        storage_type = url_to_delete.storage_type
        log.info(
            f"Deleting {storage_type} objects for company: {company}, user: {user}"
        )
        company_storage_urls_query = urls_query & Q(
            company=company, storage_type=storage_type,
        )
        delete_urls(
            urls_query=company_storage_urls_query,
            storage=storage_helpers[storage_type](company=company),
        )


def main():
    parser = ArgumentParser(description=__doc__)

    parser.parse_args()

    db.initialize()
    run_delete_loop()


if __name__ == "__main__":
    main()
