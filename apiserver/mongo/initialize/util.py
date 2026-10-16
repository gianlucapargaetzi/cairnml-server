from logging import Logger
from uuid import uuid4

from apiserver.config_repo import config
from apiserver.database.model.company import Company
from apiserver.database.model.settings import Settings, SettingKeys

log = config.logger(__file__)


def _ensure_company(company_id, company_name, log: Logger):
    company = Company.objects(id=company_id).only("id").first()
    if company:
        return company_id

    log.info(f"Creating company: {company_name}")
    company = Company(id=company_id, name=company_name)
    company.save()
    return company_id


def _ensure_uuid():
    Settings.add_value(SettingKeys.server__uuid, str(uuid4()))
