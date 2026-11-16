"""Which services each client gets, and the Python packages the generated code imports from."""

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class Target:
    services: Optional[Tuple[str, ...]]  # None: every service of the schema, so a new service is never left out
    session_package: str  # where Request, Response, NonStrictDataModel and schema_property come from

    def resolve(self, schema_services) -> Tuple[str, ...]:
        return tuple(sorted(schema_services)) if self.services is None else self.services


TARGETS: Dict[str, Target] = {
    "sdk": Target(services=None, session_package="cairnml.backend_api.session"),
}
