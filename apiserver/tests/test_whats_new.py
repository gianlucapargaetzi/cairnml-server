from pathlib import Path

from apiserver.release.whats_new import bundle, has_section, sections

CHANGELOG = """# Changelog

Intro text.

## API 2.41

One API version.

## 0.0.6

- Release notes of 0.0.6.
- Second line.

## 0.0.5

- Older notes.
"""


def test_release_sections_are_read_and_api_sections_are_left_out():
    found = sections(CHANGELOG)
    assert set(found) == {"0.0.6", "0.0.5"}
    assert found["0.0.6"] == "- Release notes of 0.0.6.\n- Second line."
    assert "One API version" not in " ".join(found.values())


def test_a_section_runs_until_the_next_heading():
    assert "Older notes" not in sections(CHANGELOG)["0.0.6"]


def test_a_component_without_a_changelog_is_empty():
    result = bundle({"server": CHANGELOG, "web": None})
    assert set(result) == {"server", "web", "sdk"}
    assert result["web"] == {} and result["sdk"] == {}
    assert result["server"]["0.0.5"] == "- Older notes."


def test_the_release_check_can_ask_for_a_version_section():
    assert has_section(CHANGELOG, "0.0.6")
    assert not has_section(CHANGELOG, "0.0.7")


def test_the_notes_of_one_release_are_its_section():
    from apiserver.release.whats_new import main
    import io, contextlib, tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp, "CHANGELOG.md")
        path.write_text(CHANGELOG)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert main(["--notes", str(path), "--version", "0.0.6"]) == 0
    assert out.getvalue().strip() == "- Release notes of 0.0.6.\n- Second line."


# The SDK's agent notes are "Agent:" lines inside the SDK's release section, with no heading: a "###" heading shows
# literally in the What's new dialog, and a "##" heading would end the section.
AGENT_CHANGELOG = """# Changelog

## 0.0.9

- SDK change.

Agent: the agent is part of this package; install it with `pip install "cairnml[agent]"`.

## 0.0.8

- Older SDK notes.
"""


def test_agent_notes_written_as_agent_lines_stay_in_the_sdk_section():
    text = sections(AGENT_CHANGELOG)["0.0.9"]
    assert "Agent: the agent is part of this package" in text
    assert "Older SDK notes" not in text
    assert has_section(AGENT_CHANGELOG, "0.0.9")

REPO = Path(__file__).resolve().parents[2]


def test_the_image_build_writes_the_bundle_itself():
    # A fresh checkout has no docker/build/whats-new.json (it is never committed): the image must generate the bundle.
    dockerfile = (REPO / "docker" / "build" / "Dockerfile").read_text()
    assert "docker/build/whats-new.json" not in dockerfile
    assert "apiserver/release/whats_new.py" in dockerfile
    assert "CHANGELOG.md" in dockerfile
    assert "!CHANGELOG.md" in (REPO / ".dockerignore").read_text().splitlines()


def test_no_script_writes_the_bundle_outside_the_image_build():
    for path in ("deploy/scripts/bootstrap.sh", ".github/workflows/docker.yml"):
        text = (REPO / path).read_text()
        assert "whats_new.py" not in text, path
