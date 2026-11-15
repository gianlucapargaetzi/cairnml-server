"""The two user documents keep the collections and databases they had before the classes were renamed
(AuthUser and UserProfile, formerly both called User). A rename must not move any data."""

from apiserver.database import Database
from apiserver.database.model.auth import AuthUser
from apiserver.database.model.user import UserProfile


def test_the_authentication_user_stays_in_the_auth_database_collection_user():
    assert AuthUser._get_collection_name() == "user"
    assert AuthUser._meta["db_alias"] == Database.auth


def test_the_profile_stays_in_the_backend_database_collection_user():
    assert UserProfile._get_collection_name() == "user"
    assert UserProfile._meta["db_alias"] == Database.backend
