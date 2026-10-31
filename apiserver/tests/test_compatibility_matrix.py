"""The compatibility matrix (docs/compatibility/matrix.md) covers the tracked areas, uses the four status values, gives a
migration path for each row, and links only to pages and headings that exist."""
import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[2] / "docs" / "compatibility"
MATRIX = DOCS / "matrix.md"

STATUSES = {"compatible", "changed", "removed", "not planned"}

# The areas the compatibility policy tracks (upstream ClearML differences that break automation or user workflows).
TRACKED_AREAS = (
    "CLI: enrolment",
    "CLI: tasks",
    "CLI: datasets",
    "CLI: parameter search",
    "CLI: debugging",
    "CLI: agent",
    "Dataset workflow",
    "API access keys",
    "Web sign-in and accounts",
    "Server auth modes",
    "File storage",
    "Agent app metering and proxy",
)

LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _rows():
    rows = []
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or set(line) <= {"|", "-", " "}:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        rows.append(cells)
    header, *body = rows
    assert header == ["Feature area", "Upstream (ClearML)", "CairnML", "Status", "Migration"]
    return body


def _anchor(heading: str) -> str:
    text = heading.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def test_every_tracked_area_has_a_row():
    areas = [row[0] for row in _rows()]
    for area in TRACKED_AREAS:
        assert area in areas, f"no row for {area}"


def test_every_row_has_a_known_status_and_a_migration_path():
    for area, upstream, cairnml, status, migration in _rows():
        assert status in STATUSES, f"{area}: unknown status {status!r}"
        assert upstream and cairnml and migration, f"{area}: an empty cell"


def test_every_link_points_at_an_existing_page_and_heading():
    for row in _rows():
        for target in LINK.findall(" | ".join(row)):
            page, _, fragment = target.partition("#")
            path = DOCS / page
            assert path.is_file(), f"{row[0]}: {page} does not exist"
            if fragment:
                headings = [_anchor(line.lstrip("#")) for line in path.read_text(encoding="utf-8").splitlines()
                            if line.startswith("#")]
                assert fragment in headings, f"{row[0]}: no heading #{fragment} in {page}"


@pytest.mark.parametrize("page", ["README.md", "matrix.md", "cli-migration.md", "dataset-migration.md",
                                  "auth-and-storage.md", "agent-metering.md"])
def test_the_compatibility_pages_exist(page):
    assert (DOCS / page).is_file()


def test_the_agent_metering_row_is_not_planned():
    row = next(r for r in _rows() if r[0] == "Agent app metering and proxy")
    assert row[3] == "not planned"
