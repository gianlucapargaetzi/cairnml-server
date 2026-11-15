"""Which services each client gets, and the Python packages the generated code imports from."""

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class Target:
    services: Tuple[str, ...]
    session_package: str  # where Request, Response, NonStrictDataModel and schema_property come from


TARGETS = {
    "sdk": Target(services=("queues",), session_package="cairnml.backend_api.session"),
}
