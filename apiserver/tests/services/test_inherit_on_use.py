"""A run that records a dataset it uses shares that dataset with its project when it records it (not when it finishes)."""
from types import SimpleNamespace

from apiserver.bll.task import utils as task_utils
from apiserver.services import tasks as svc


def item(section, name):
    return SimpleNamespace(section=section, name=name, value="x")


def run_edit(monkeypatch, hyperparams):
    shared = []
    monkeypatch.setattr(svc, "_assert_tasks_access", lambda *args, **kwargs: None)
    monkeypatch.setattr(svc.HyperParams, "edit_params", staticmethod(lambda *args, **kwargs: 1))
    monkeypatch.setattr(task_utils, "_publish_datasets", lambda task: shared.append((task.company, task.id)))
    request = SimpleNamespace(task="run1", hyperparams=hyperparams, replace_hyperparams="none", force=False)
    call = SimpleNamespace(identity=SimpleNamespace(user="u1", company="c1", role="user"), result=SimpleNamespace(data=None))
    svc.edit_hyper_params(call, "c1", request)
    return shared


def test_recording_a_dataset_shares_it_with_the_run_project_at_once(monkeypatch):
    assert run_edit(monkeypatch, [item("Datasets", "cars/repo")]) == [("c1", "run1")]


def test_other_parameters_share_nothing(monkeypatch):
    assert run_edit(monkeypatch, [item("General", "lr")]) == []
