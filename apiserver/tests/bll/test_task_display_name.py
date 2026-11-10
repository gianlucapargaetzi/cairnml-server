from unittest import TestCase

from apiserver.database.model.task.task import Task
from apiserver.services import tasks as tasks_service


class TestTaskDisplayName(TestCase):
    """The display name labels one experiment. It never takes part in the task's identity (its name)."""

    def test_user_may_set_it(self):
        self.assertIn("display_name", Task.user_set_allowed())

    def test_it_is_a_create_and_update_field(self):
        self.assertIn("display_name", tasks_service.create_fields)

    def test_it_is_searched_with_the_name(self):
        self.assertIn("display_name", Task.get_all_query_options.pattern_fields)

    def test_it_is_optional_and_bounded(self):
        field = Task.display_name
        self.assertFalse(field.required)
        self.assertEqual(field.max_length, 200)
        self.assertEqual(field.min_length, 1)

    def test_name_stays_required(self):
        self.assertTrue(Task.name.required)
