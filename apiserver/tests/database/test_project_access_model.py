from datetime import datetime
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility

def test_new_project_defaults_to_private_with_no_members():
    project = Project(id="p1", company="c1", user="u1", name="Foo", basename="Foo", created=datetime.utcnow())
    assert project.visibility == ProjectVisibility.private
    assert project.members == []

def test_a_member_can_be_appended_and_read_back():
    project = Project(id="p1", company="c1", user="u1", name="Foo", basename="Foo", created=datetime.utcnow())
    project.members.append(ProjectMember(user="u2", role=ProjectRole.collaborator, added=datetime.utcnow(), added_by="u1"))
    assert project.members[0].user == "u2"
    assert project.members[0].role == ProjectRole.collaborator
