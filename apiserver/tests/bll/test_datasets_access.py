from apiserver.bll import datasets as bll

OWNER = "u1"
OTHER = "u2"


def dataset(**overrides) -> dict:
    values = {"id": "d1", "name": "cars", "owner": OWNER, "visibility": "user", "projects": [], "credential": None}
    values.update(overrides)
    return values


class TestVisibleTo:
    def test_user_visibility_is_the_owner_and_admins_only(self):
        assert bll.visible_to(dataset(), OWNER, set(), False)
        assert bll.visible_to(dataset(), OTHER, set(), True)
        assert not bll.visible_to(dataset(), OTHER, set(), False)

    def test_projects_visibility_is_the_owner_admins_and_members_of_an_allocated_project(self):
        shared = dataset(visibility="projects", projects=["p1"])
        assert bll.visible_to(shared, OTHER, {"p1"}, False)
        assert not bll.visible_to(shared, OTHER, {"p2"}, False)
        assert bll.visible_to(shared, OWNER, set(), False)

    def test_public_visibility_is_every_user(self):
        assert bll.visible_to(dataset(visibility="Public"), OTHER, set(), False)


class TestCredentialState:
    def test_a_public_dataset_needs_no_credential(self):
        assert bll.credential_state(dataset(credential=None), set()) == ("ok", "")

    def test_a_dataset_whose_credential_is_still_there_is_ok(self):
        assert bll.credential_state(dataset(credential="a1"), {"a1"}) == ("ok", "")

    def test_a_dataset_whose_credential_is_gone_has_no_access_with_the_banner_text(self):
        assert bll.credential_state(dataset(credential="a1"), set()) == (
            "no_access", "no access to dataset cars")


class TestCanManage:
    def test_the_owner_and_admins_may_manage(self):
        assert bll.can_manage(dataset(), OWNER, False)
        assert bll.can_manage(dataset(), OTHER, True)

    def test_another_user_may_not_manage(self):
        assert not bll.can_manage(dataset(), OTHER, False)
