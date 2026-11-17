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
