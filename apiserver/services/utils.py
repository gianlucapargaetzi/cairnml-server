from apiserver.apimodels.organization import Filter
from apiserver.bll.project import project_ids_with_children
from apiserver.utilities.parameter_key_escaper import ParameterKeyEscaper


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


def escape_metadata(document: dict):
    """
    Escape special characters in metadata keys
    """
    metadata = document.get("metadata")
    if not metadata:
        return

    document["metadata"] = {ParameterKeyEscaper.escape(k): v for k, v in metadata.items()}
