from types import SimpleNamespace

from apiserver.bll.task.task_bll import folder_root_id
from apiserver.bll.task.utils import deleted_prefix


def tasks(**parents):
    """A lookup over tasks given as id -> parent id (None for a task with no parent)."""
    store = {task_id: SimpleNamespace(id=task_id, parent=parent) for task_id, parent in parents.items()}
    return lambda task_id: store.get(task_id)


def test_a_source_with_no_parent_is_its_own_root():
    assert folder_root_id("source", tasks(source=None)) == "source"


def test_a_source_under_a_root_gets_the_root():
    assert folder_root_id("source", tasks(source="root", root=None)) == "root"


def test_a_clone_of_a_clone_gets_the_root_not_the_intermediate_clone():
    lookup = tasks(c3="c2", c2="c1", c1="root", root=None)
    assert folder_root_id("c3", lookup) == "root"


def test_an_explicit_parent_is_resolved_to_its_root():
    lookup = tasks(source="clone", clone="root", root=None)
    assert folder_root_id("clone", lookup) == "root"


def test_a_deleted_parent_leaves_the_source_as_the_root():
    lookup = tasks(source=deleted_prefix + "old-root", root=None)
    assert folder_root_id("source", lookup) == "source"


def test_a_parent_that_cannot_be_read_stops_the_walk_at_the_last_readable_task():
    lookup = tasks(source="gone")
    assert folder_root_id("source", lookup) == "source"


def test_a_loop_stops_the_walk():
    lookup = tasks(a="b", b="a")
    assert folder_root_id("a", lookup) in ("a", "b")


def test_an_unreadable_start_is_returned_as_it_is():
    assert folder_root_id("missing", tasks()) == "missing"
