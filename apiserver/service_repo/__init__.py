from typing import Text, Sequence, Type

from inspect import signature
from jsonmodels import models

from .apicall import APICall, APICallResult
from .endpoint import EndpointFunc, Endpoint
from .api_version import API_VERSION
from .service_repo import ServiceRepo


__all__ = ["ServiceRepo", "APICall", "endpoint", "API_VERSION"]


def endpoint(
    name: Text,
    min_version: Text = API_VERSION,
    required_fields: Sequence[Text] = None,
    request_data_model: Type[models.Base] = None,
    response_data_model: Type[models.Base] = None,
    validate_schema=False,
):
    """Endpoint decorator, used to declare a method as an endpoint handler"""

    def decorator(f: EndpointFunc) -> EndpointFunc:
        # endpoints take (call, company, request_model); the request model is the annotation of the last argument
        func = f
        sig = signature(f)
        try:
            sig.bind(None, None, None)
        except TypeError as ex:
            raise TypeError(f"endpoint {name}: handler {f.__qualname__} must take (call, company, request)") from ex
        nonlocal request_data_model
        if len(sig.parameters) == 3 and request_data_model is None:
            last_arg = list(sig.parameters.items())[-1][1]
            if last_arg.annotation != last_arg.empty:
                request_data_model = last_arg.annotation

        ServiceRepo.register(
            Endpoint(
                name=name,
                func=func,
                min_version=min_version,
                required_fields=required_fields,
                request_data_model=request_data_model,
                response_data_model=response_data_model,
                validate_schema=validate_schema,
            )
        )

        return func

    return decorator


# APICallResult is re-exported for the callers of service_repo.
__all__ = ["APICall", "APICallResult"]
