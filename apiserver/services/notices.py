# apiserver/services/notices.py
"""The notices of inherited datasets (see bll/access): the runs of the signed-in user that shared a dataset with a project
they still use, and the dismissal of one."""

from apiserver.bll import access as access_bll
from apiserver.service_repo import APICall, endpoint


@endpoint("access.notices")
def notices(call: APICall, company: str, _request):
    call.result.data = {"notices": access_bll.notices(company, call.identity.user)}


@endpoint("access.dismiss_notice")
def dismiss_notice(call: APICall, company: str, _request):
    data = call.data or {}
    access_bll.dismiss_notice(company, call.identity.user, data.get("id", ""))
