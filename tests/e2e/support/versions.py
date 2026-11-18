"""Every API version the suite names, each with its reason. Nothing else in the suite writes a version literal.

Requests that name no version go to the server's own latest level, which is the default (api.Api). The entries below
are levels the suite needs on purpose.
"""

# The project membership and visibility endpoints (projects.add_member and the family) exist from this level.
MEMBERSHIP = "2.37"

# events.get_task_log pages a task's console without an Elasticsearch scroll from this level; an older level opens one
# scroll per call and leaves it open.
TASK_LOG_PAGING = "2.9"
