from datetime import datetime, timedelta
from time import sleep
from uuid import uuid4

from apiserver.tests.automated import TestService


class TestProjectWorkloads(TestService):
    def setUp(self, **kwargs):
        super().setUp(**kwargs)
        self.user = self.api.users.get_current_user().user

    def test_workloads(self):
        project_name = f"Project Workload {uuid4()}"
        project = self._temp_project(project_name)
        child_project_name = f"{project_name}/Child1"
        child_project = self._temp_project(child_project_name)
        task_root_running = self._create_temp_queued_task(
            task_name="workloads test1", project=project
        )
        self.api.tasks.started(task=task_root_running)
        task_child_failed = self._create_temp_queued_task(
            task_name="workloads test2", project=child_project
        )
        self.api.tasks.started(task=task_child_failed)
        task_child_completed = self._temp_task(
            task_name="workloads test3", is_development=True, project=child_project
        )
        self.api.tasks.started(task=task_child_completed)
        task_child_not_running = self._create_temp_queued_task(
            task_name="workloads test4", project=child_project
        )

        sleep(5)
        self.api.tasks.failed(task=task_child_failed)
        self.api.tasks.stopped(task=task_child_completed)

        from_date = (datetime.now().astimezone() - timedelta(days=5)).isoformat()
        to_date = datetime.now().astimezone().isoformat()
        res = self.api.organization.get_project_workloads(
            projects=[project],
            from_date=from_date,
            to_date=to_date,
            include_development=True,
        )
        pass

    delete_params = dict(can_fail=True, force=True)

    def _temp_project(self, name, **kwargs):
        return self.create_temp(
            "projects",
            delete_params=self.delete_params,
            name=name,
            description="",
            **kwargs,
        )

    def _temp_task(self, task_name, is_development=False, **kwargs):
        task_input = dict(
            name=task_name,
            type="training",
            script={"repository": "test", "entry_point": "test"},
            system_tags=["development"] if is_development else None,
            delete_params=self.delete_params,
            **kwargs,
        )
        return self.create_temp("tasks", **task_input)

    def _create_temp_queued_task(self, task_name, **kwargs) -> str:
        task_id = self._temp_task(task_name, **kwargs)
        self.api.tasks.enqueue(task=task_id)
        return task_id
