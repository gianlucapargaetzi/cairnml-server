from apiserver.service_repo import APICall, endpoint


@endpoint("debug.ping")
def ping(call: APICall, _, __):
    res = {"msg": "CairnML server"}
    if call.data:
        res.update(call.data)
    call.result.data = res
