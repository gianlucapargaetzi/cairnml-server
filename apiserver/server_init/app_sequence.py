import atexit
from hashlib import md5

from flask import Flask
from flask_compress import Compress
from flask_cors import CORS
from packaging.version import Version

from apiserver.bll.queue.queue_metrics import MetricsRefresher
from apiserver.bll.task.non_responsive_tasks_watchdog import NonResponsiveTasksWatchdog
from apiserver.database import db
from apiserver.bll.statistics.stats_reporter import StatisticsReporter
from apiserver.config import info
from apiserver.config_repo import config
from apiserver.idp.legacy import check_no_legacy_settings
from apiserver.idp.secrets import session_secret
from apiserver.server_init.cors import cors_settings
from apiserver.elastic.initialize import (
    init_es_data,
    check_elastic_empty,
    ElasticConnectionError,
)
from apiserver.mongo.initialize import (
    init_mongo_data,
    pre_populate_data,
    check_mongo_empty,
    get_last_server_version,
)
from apiserver.server_init.request_handlers import RequestHandlers
from apiserver.service_repo import ServiceRepo
from apiserver.sync import distributed_lock
from apiserver.updates import check_updates_thread
from apiserver.utilities.env import get_bool
from apiserver.server_init import datachunk_download

log = config.logger(__file__)


class AppSequence:
    def __init__(self, app: Flask):
        self.app = app

    def start(self, request_handlers: RequestHandlers):
        log.info("################ API Server initializing #####################")
        check_no_legacy_settings()
        self._configure()
        self._init_dbs()
        self._load_services()
        self._start_worker()
        atexit.register(self._on_worker_stop)
        self._attach_request_handlers(request_handlers)

    def _attach_request_handlers(self, request_handlers: RequestHandlers):
        self.app.before_request(request_handlers.before_request)
        self.app.after_request(request_handlers.after_request)
        datachunk_download.register(self.app)

    def _configure(self):
        CORS(self.app, **cors_settings())

        if get_bool("CAIRNML_SKIP_COMPRESS_STREAM", default=False):
            self.app.config["COMPRESS_STREAMS"] = False

        if get_bool("CAIRNML_COMPRESS_RESP", default=True):
            Compress(self.app)

        self.app.config["SECRET_KEY"] = session_secret()
        self.app.config["JSONIFY_PRETTYPRINT_REGULAR"] = config.get(
            "apiserver.pretty_json"
        )

    @staticmethod
    def _get_db_instance_key() -> str:
        """build a key that uniquely identifies specific mongo instance"""
        hosts_string = ";".join(sorted(db.get_hosts()))
        return "db_init_" + md5(hosts_string.encode()).hexdigest()

    def _init_dbs(self):
        db.initialize()

        with distributed_lock(
            name=self._get_db_instance_key(),
            timeout=config.get("apiserver.db_init_timout", 120),
        ):
            upgrade_monitoring = config.get(
                "apiserver.elastic.upgrade_monitoring.v16_migration_verification", True
            )
            try:
                empty_es = check_elastic_empty()
            except ElasticConnectionError as err:
                if not upgrade_monitoring:
                    raise
                log.error(err)
                info.es_connection_error = True

            empty_db = check_mongo_empty()
            if (
                upgrade_monitoring
                and not empty_db
                and (info.es_connection_error or empty_es)
                and get_last_server_version() < Version("0.16.0")
            ):
                log.info("ES database seems not migrated")
                info.missed_es_upgrade = True

            if info.es_connection_error and not info.missed_es_upgrade:
                raise Exception(
                    "Error starting server: failed connecting to ElasticSearch service"
                )

            if not info.missed_es_upgrade:
                init_es_data()
                init_mongo_data()

        if (
            not info.missed_es_upgrade
            and empty_db
            and config.get("apiserver.pre_populate.enabled", False)
        ):
            pre_populate_data()

    def _load_services(self):
        ServiceRepo.load("services")
        log.info(f"Exposed Services: {' '.join(ServiceRepo.endpoint_names())}")

    def _start_worker(self):
        check_updates_thread.start()
        StatisticsReporter.start()
        MetricsRefresher.start()
        NonResponsiveTasksWatchdog.start()
        self._start_idp_sync()

    @staticmethod
    def _start_idp_sync():
        # Off until the identity provider is configured for the deployment (apiserver.idp.sync_enabled).
        if not config.get("apiserver.idp.sync_enabled", False):
            return
        from apiserver.idp.directory import build_from_config
        from apiserver.idp.user_sync import UserSyncThread

        UserSyncThread(
            build_from_config(),
            config.get("apiserver.idp.admin_role"),
            config.get("apiserver.idp.sync_interval_sec"),
        ).start()

    def _on_worker_stop(self):
        pass
