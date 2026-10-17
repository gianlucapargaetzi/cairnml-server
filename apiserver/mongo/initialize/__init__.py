from pathlib import Path
from typing import Sequence, Union

from apiserver.config_repo import config
from apiserver.config.info import get_default_company
from .migration import _apply_migrations, check_mongo_empty, get_last_server_version
from .pre_populate import PrePopulate
from .util import _ensure_company, _ensure_uuid

log = config.logger(__package__)


def _pre_populate(company_id: str, zip_file: str):
    if not zip_file or not Path(zip_file).is_file():
        msg = f"Invalid pre-populate zip file: {zip_file}"
        if config.get("apiserver.pre_populate.fail_on_error", False):
            log.error(msg)
            raise ValueError(msg)
        else:
            log.warning(msg)
    else:
        log.info(f"Pre-populating using {zip_file}")

        PrePopulate.import_from_zip(zip_file)


def _resolve_zip_files(zip_files: Union[Sequence[str], str]) -> Sequence[str]:
    if isinstance(zip_files, str):
        zip_files = [zip_files]
    for p in map(Path, zip_files):
        if p.is_file():
            yield p
        if p.is_dir():
            yield from p.glob("*.zip")
        log.warning(f"Invalid pre-populate entry {str(p)}, skipping")


def pre_populate_data():
    for zip_file in _resolve_zip_files(config.get("apiserver.pre_populate.zip_files")):
        _pre_populate(company_id=get_default_company(), zip_file=zip_file)

    PrePopulate.update_featured_projects_order()


def init_mongo_data():
    try:
        _apply_migrations(log)

        _ensure_uuid()

        _ensure_company(get_default_company(), "clearml", log)

    except Exception as ex:
        log.exception(f"Failed initializing mongodb: {str(ex)}")
