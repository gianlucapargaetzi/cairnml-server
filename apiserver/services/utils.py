from typing import Union, Sequence, Tuple

from apiserver.apimodels.organization import Filter
from apiserver.bll.project import project_ids_with_children
from apiserver.bll.util import validate_tags
from apiserver.database.utils import partition_tags
from apiserver.service_repo import APICall
from apiserver.utilities.parameter_key_escaper import ParameterKeyEscaper
from apiserver.utilities.partial_version import PartialVersion


def process_include_subprojects(call_data: dict):
    include_subprojects = call_data.pop("include_subprojects", False)
    project_ids = call_data.get("project")
    if not project_ids or not include_subprojects:
        return

    if not isinstance(project_ids, list):
        project_ids = [project_ids]
    call_data["project"] = project_ids_with_children(project_ids)


def get_tags_filter_dictionary(input_: Filter) -> dict:
    if not input_:
        return {}

    return {field: vals for field, vals in (("tags", input_.tags), ("system_tags", input_.system_tags)) if vals}


def sort_tags_response(ret: dict) -> dict:
    return {field: sorted(vals) for field, vals in ret.items()}


def conform_output_tags(call: APICall, documents: Union[dict, Sequence[dict]]):
    """
    Make sure that tags are always returned sorted
    For old clients both tags and system tags are returned in 'tags' field
    """
    if isinstance(documents, dict):
        documents = [documents]

    merge_tags = call.requested_endpoint_version < PartialVersion("2.3")
    for doc in documents:
        if merge_tags:
            system_tags = doc.get("system_tags")
            if system_tags:
                doc["tags"] = list(set(doc.get("tags", [])) | set(system_tags))

        for field in ("system_tags", "tags"):
            tags = doc.get(field)
            if tags:
                doc[field] = sorted(tags)


def conform_tag_fields(call: APICall, document: dict, validate=False):
    """
    Upgrade old client tags in place
    """
    if "tags" in document:
        tags, system_tags = conform_tags(call, document["tags"], document.get("system_tags"), validate)
        if tags != document.get("tags"):
            document["tags"] = tags
        if system_tags != document.get("system_tags"):
            document["system_tags"] = system_tags


def conform_tags(call: APICall, tags: Sequence, system_tags: Sequence, validate=False) -> Tuple[Sequence, Sequence]:
    """
    Make sure that 'tags' from the old SDK clients
    are correctly split into 'tags' and 'system_tags'
    Make sure that there are no duplicate tags
    """
    if validate:
        validate_tags(tags, system_tags)
    if call.requested_endpoint_version < PartialVersion("2.3"):
        tags, system_tags = _upgrade_tags(call, tags, system_tags)
    return tags, system_tags


def _upgrade_tags(call: APICall, tags: Sequence, system_tags: Sequence):
    if tags is not None and not system_tags:
        service_name = call.endpoint_name.partition(".")[0]
        entity = service_name[:-1] if service_name.endswith("s") else service_name
        return partition_tags(entity, tags)

    return tags, system_tags


def escape_metadata(document: dict):
    """
    Escape special characters in metadata keys
    """
    metadata = document.get("metadata")
    if not metadata:
        return

    document["metadata"] = {ParameterKeyEscaper.escape(k): v for k, v in metadata.items()}


def unescape_metadata(call: APICall, documents: Union[dict, Sequence[dict]]):
    """
    Unescape special characters in metadata keys
    """
    if isinstance(documents, dict):
        documents = [documents]

    old_client = call.requested_endpoint_version <= PartialVersion("2.16")
    for doc in documents:
        if old_client and "metadata" in doc:
            doc["metadata"] = []
            continue

        metadata = doc.get("metadata")
        if not metadata:
            continue

        doc["metadata"] = {ParameterKeyEscaper.unescape(k): v for k, v in metadata.items()}
