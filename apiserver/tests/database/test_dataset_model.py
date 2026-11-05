from datetime import datetime

import pytest
from mongoengine import ValidationError

from apiserver.database.model.dataset import Dataset


def make(**overrides) -> Dataset:
    values = dict(id="d1", company="c1", kind="repo", ref="acme/cars", name="cars", owner="u1",
                  visibility="user", created=datetime.utcnow())
    values.update(overrides)
    return Dataset(**values)


def test_a_new_dataset_is_owner_only_with_no_credential_and_ok_state():
    dataset = make()
    dataset.validate()
    assert dataset.projects == []
    assert dataset.credential is None
    assert dataset.state == "ok"


@pytest.mark.parametrize("visibility", ["user", "projects", "Public"])
def test_the_three_visibilities_are_accepted(visibility):
    make(visibility=visibility).validate()


def test_another_visibility_word_is_refused():
    with pytest.raises(ValidationError):
        make(visibility="global").validate()


def test_a_state_other_than_ok_or_no_access_is_refused():
    with pytest.raises(ValidationError):
        make(state="broken").validate()
