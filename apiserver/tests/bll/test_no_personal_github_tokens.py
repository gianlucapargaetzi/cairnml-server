"""GitHub is reached through a GitHub App only: a personal access token is refused when it is added."""
import pytest

from apiserver.apierrors import errors
from apiserver.bll import user_access


def test_a_personal_token_for_github_is_refused_when_it_is_added():
    with pytest.raises(errors.bad_request.ValidationError, match="no longer accepted for GitHub"):
        user_access.add_token("c1", "u1", "my token", "github.com", "", "ghp_secret")
