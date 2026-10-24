from jsonmodels import models, fields


class ProjectMemberRequest(models.Base):
    project = fields.StringField(required=True)
    user = fields.StringField(required=True)


class AddMemberRequest(ProjectMemberRequest):
    role = fields.StringField(required=True)


class UpdateMemberRoleRequest(ProjectMemberRequest):
    role = fields.StringField(required=True)


class RemoveMemberRequest(ProjectMemberRequest):
    pass


class GetMembersRequest(models.Base):
    project = fields.StringField(required=True)


class SetVisibilityRequest(models.Base):
    project = fields.StringField(required=True)
    visibility = fields.StringField(required=True)


class TransferOwnershipRequest(models.Base):
    project = fields.StringField(required=True)
    new_owner = fields.StringField(required=True)
