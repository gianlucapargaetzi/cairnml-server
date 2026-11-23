"""The release workflow releases the server, the SDK (which includes the agent) and the web app, then the image.

The agent has no job, no edge and no input of its own: the server image needs the server, the SDK and the web app.
"""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "release.yml"


def needs_of(job: dict) -> list:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def test_the_release_workflow_has_no_agent_job_edge_or_input():
    text = WORKFLOW.read_text(encoding="utf-8")
    jobs = yaml.safe_load(text)["jobs"]
    assert "agent" not in jobs
    for name, job in jobs.items():
        assert "agent" not in needs_of(job), name
        assert "agent" not in str(job.get("if", "")), name
    assert "agent_sha" not in text and "agent_ref" not in text


def test_the_server_image_needs_the_server_the_sdk_and_the_web_app_only():
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    assert set(needs_of(jobs["image"])) == {"check", "approve", "server", "sdk", "web"}
