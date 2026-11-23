"""The release plan in release-check.yml plans the server, the SDK and the web app, and nothing about an agent.

The test runs the plan script itself (the heredoc of the workflow) against stub repositories, with PyPI answering 404.
"""

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "release-check.yml"

# Runs the plan script read from argv[1] with PyPI stubbed: every package name is absent.
RUNNER = """
import sys, urllib.error, urllib.request
def offline(url, *args, **kwargs):
    raise urllib.error.HTTPError(url, 404, "not found", None, None)
urllib.request.urlopen = offline
script = open(sys.argv[1], encoding="utf-8").read()
exec(compile(script, sys.argv[1], "exec"), {"__name__": "__main__"})
"""


def plan_script() -> str:
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "python - <<'PY'")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "PY")
    return textwrap.dedent("\n".join(lines[start + 1 : end])) + "\n"


def commit(repo: Path, files: dict, message: str) -> None:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git = ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid"]
    subprocess.run([*git, "add", "--", *files], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-q", "-m", message], check=True, capture_output=True)


def init(repo: Path) -> None:
    repo.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True, capture_output=True)


def changelog(version: str) -> str:
    return f"# Changelog\n\n## {version}\n\nNotes for {version}.\n"


def stub_workspace(root: Path) -> Path:
    """A server repository with an SDK inside it at sdk/, a web app at web/ and an agent at agent/. The server is
    releasing 0.0.6, the SDK is releasing 0.0.9, the web app has no release tag yet."""
    server = root / "server"
    init(server)
    shutil.copy(REPO / "apiserver" / "release" / "whats_new.py", root / "whats_new.py")
    release_module = server / "apiserver" / "release"
    release_module.mkdir(parents=True)
    shutil.copy(root / "whats_new.py", release_module / "whats_new.py")
    commit(
        server,
        {"apiserver/version.py": '__version__ = "0.0.5"\n', "CHANGELOG.md": changelog("0.0.5")},
        "release 0.0.5",
    )
    subprocess.run(["git", "-C", str(server), "tag", "v0.0.5"], check=True, capture_output=True)
    commit(
        server,
        {"apiserver/version.py": '__version__ = "0.0.6"\n', "CHANGELOG.md": changelog("0.0.6")},
        "bump to 0.0.6",
    )

    sdk = server / "sdk"
    init(sdk)
    commit(sdk, {"cairnml/version.py": '__version__ = "0.0.8"\n', "CHANGELOG.md": changelog("0.0.8")}, "release 0.0.8")
    subprocess.run(["git", "-C", str(sdk), "tag", "v0.0.8"], check=True, capture_output=True)
    commit(sdk, {"cairnml/version.py": '__version__ = "0.0.9"\n', "CHANGELOG.md": changelog("0.0.9")}, "bump to 0.0.9")

    web = server / "web"
    init(web)
    commit(web, {"package.json": '{"name": "web", "version": "0.0.3"}\n'}, "web app")

    # the old separate agent repository is still checked out beside them: the plan must not turn it into a row
    agent = server / "agent"
    init(agent)
    commit(agent, {"cairnml_agent/version.py": '__version__ = "0.0.1"\n'}, "agent")
    subprocess.run(["git", "-C", str(agent), "tag", "v0.0.1"], check=True, capture_output=True)
    return server


def run_plan(server: Path, tmp_path: Path, require_release: str = "true") -> tuple:
    script = tmp_path / "plan.py"
    script.write_text(plan_script(), encoding="utf-8")
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "REQUIRE_RELEASE": require_release,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_OUTPUT": str(output),
    }
    result = subprocess.run(
        [sys.executable, "-c", RUNNER, str(script)], cwd=server, env=env, capture_output=True, text=True
    )
    return result, dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())


def test_the_release_plan_has_server_sdk_and_web_rows_and_no_agent(tmp_path):
    server = stub_workspace(tmp_path)
    result, out = run_plan(server, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("PROBLEM") == 0, result.stdout
    assert {key.rsplit("_", 1)[0] for key in out} == {"server", "sdk", "web"}
    assert out["server_release"] == "true" and out["server_version"] == "0.0.6"
    assert out["sdk_release"] == "true" and out["sdk_version"] == "0.0.9"
    assert out["web_release"] == "false"
    assert not any(key.startswith("agent") for key in out)
