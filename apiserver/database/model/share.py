"""A grant of one resource (a queue or a repository) to a set of projects, to one user (queues only), or to everyone
(public, read only). A share with an empty project and an empty user is a public share: for a repository it is what makes an
entry shared (owned by the admin). A project share of a repository is the link that lists it in the project.
See docs/superpowers/specs/2026-11-05-access-model-design.md and 2026-11-11-unified-repositories-design.md."""

from mongoengine import BooleanField, DateTimeField, Document, ListField, StringField, ValidationError

from apiserver.database import Database, strict
from apiserver.database.model import DbModelMixin

RESOURCE_TYPES = ("queue", "repository")
SCOPES = ("projects", "public", "user")
LEVELS = ("read", "read_write", "collaborator")

LEVELS_BY_TYPE = {
    "queue": ("read", "read_write", "collaborator"),
    "repository": ("read", "collaborator"),
}
""" The levels a share may give on each resource type """

PUBLIC_LEVELS = ("read",)
""" A public share gives read only """

USER_TYPES = ("queue",)
""" A user share (a per-user grant with no project) is valid for these types """

USER_LEVELS = ("read",)
""" A user share gives read only """


class Share(DbModelMixin, Document):
    meta = {
        "db_alias": Database.backend,
        "strict": strict,
        "collection": "share",
        "indexes": [
            {
                "fields": ["company", "resource_type", "resource_id", "project", "user"],
                "unique": True,
            },
        ],
    }

    id = StringField(primary_key=True)
    company = StringField(required=True)
    resource_type = StringField(required=True, choices=RESOURCE_TYPES)
    resource_id = StringField(required=True)
    scope = StringField(required=True, choices=SCOPES)
    project = StringField(default="")
    """ the root project id; required for the 'projects' scope, empty for 'public' and 'user' """
    user = StringField(default="")
    """ the user the share is granted to; required for the 'user' scope, empty otherwise """
    level = StringField(required=True, choices=LEVELS)
    granted_by = StringField(required=True)
    inherited = BooleanField(default=False)
    run_id = StringField()
    dismissed_by = ListField(StringField(), default=list)
    """ the users who dismissed the notice of an inherited share (see bll/access.notices) """
    created = DateTimeField()

    def clean(self):
        super().clean()
        self._check_scope()
        self._check_level()

    def _check_scope(self):
        if self.scope == "projects":
            if not self.project:
                raise ValidationError("a projects share needs a project")
            if self.user:
                raise ValidationError("a projects share must not carry a user")
        elif self.scope == "public":
            if self.project or self.user:
                raise ValidationError("a public share must not carry a project or a user")
        elif self.scope == "user":
            if not self.user:
                raise ValidationError("a user share needs a user")
            if self.project:
                raise ValidationError("a user share must not carry a project")
            if self.resource_type not in USER_TYPES:
                raise ValidationError("a user share is valid only for a queue")

    def _check_level(self):
        if self.scope == "public":
            allowed = PUBLIC_LEVELS
        elif self.scope == "user":
            allowed = USER_LEVELS
        else:
            allowed = LEVELS_BY_TYPE.get(self.resource_type, ())
        if self.level not in allowed:
            raise ValidationError(
                f"level {self.level} is not allowed for a {self.scope} share on a {self.resource_type}"
            )
