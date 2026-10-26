"""The suite's report: one row per test, with its name, what it checks, what it expects, its outcome, and whether it
passed. Written at the end of every session to tests/e2e/reports/ (Markdown and HTML).

A test's description is the first paragraph of its docstring. Its expected behaviour is the text of the docstring's
"Expect:" paragraph. A test without one is listed as not written, so the gap shows in the report.
"""

import html
import inspect
from datetime import datetime
from pathlib import Path

REPORT_DIR = Path(__file__).resolve().parents[1] / "reports"
MISSING_EXPECTATION = "(not written: add an Expect: paragraph to the test's docstring)"

_items = {}   # node id -> item
_rows = {}    # node id -> row being built from the phases of the test
_started = datetime.now()


def split_docstring(doc: str):
    """(description, expected) from a docstring. The description is its first paragraph; the expected behaviour is
    the paragraph that starts with 'Expect:'."""
    description, expected = "", []
    for paragraph in inspect.cleandoc(doc or "").split("\n\n"):
        flat = " ".join(paragraph.split())
        if flat.startswith("Expect:"):
            expected.append(flat[len("Expect:"):].strip())
        elif not description:
            description = flat
    return description, " ".join(expected)


def collect(items) -> None:
    for item in items:
        _items[item.nodeid] = item


def _short_failure(longrepr) -> str:
    text = str(longrepr).strip()
    lines = text.splitlines()
    for line in lines:
        if line.startswith("E   "):
            return line[4:].strip()[:300]
    return lines[-1][:300] if lines else "failed"


def record(report) -> None:
    """Called for every phase of every test (setup, call, teardown). A test's outcome is its worst phase."""
    row = _rows.setdefault(report.nodeid, {"nodeid": report.nodeid, "outcome": "passed", "message": "",
                                           "duration": 0.0})
    row["duration"] += report.duration
    if report.outcome == "failed":
        row["outcome"] = "error" if report.when != "call" else "failed"
        row["message"] = _short_failure(report.longrepr)
    elif report.outcome == "skipped" and row["outcome"] == "passed":
        row["outcome"] = "skipped"
        reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
        row["message"] = str(reason)


def _rows_for_report():
    result = []
    for nodeid, item in _items.items():
        description, expected = split_docstring(getattr(getattr(item, "function", None), "__doc__", ""))
        row = _rows.get(nodeid, {"outcome": "not run", "message": "", "duration": 0.0})
        result.append({
            "module": nodeid.split("::")[0],
            "name": item.name,
            "description": description or "(no description written)",
            "expected": expected or MISSING_EXPECTATION,
            "outcome": row["message"] if row["outcome"] in ("failed", "error", "skipped")
            else ("as expected" if row["outcome"] == "passed" else "not run"),
            "status": {"passed": "PASS", "failed": "FAIL", "error": "ERROR", "skipped": "SKIP"}.get(
                row["outcome"], "NOT RUN"),
            "duration": row["duration"],
        })
    return result


def write() -> None:
    if not _rows:
        return  # no test ran in this session (a collect-only run): nothing to report
    rows = _rows_for_report()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _started.strftime("%Y-%m-%d %H:%M")
    counts = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    summary = ", ".join(f"{count} {status.lower()}" for status, count in sorted(counts.items()))
    (REPORT_DIR / "e2e-report.md").write_text(_markdown(rows, stamp, summary))
    (REPORT_DIR / "e2e-report.html").write_text(_html(rows, stamp, summary))


def _markdown(rows, stamp, summary) -> str:
    lines = [f"# End-to-end report, {stamp}", "", f"{len(rows)} tests: {summary}, total {_total(rows)}", "",
             "| Test | Description | Expected behaviour | Outcome | Pass/fail | Duration |", "|---|---|---|---|---|---|"]
    for row in rows:
        cells = [f"`{row['name']}`", row["description"], row["expected"], row["outcome"], row["status"],
                 _seconds(row["duration"])]
        lines.append("| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |")
    return "\n".join(lines) + "\n"


def _seconds(value: float) -> str:
    return f"{value:.1f} s"


def _total(rows) -> str:
    """The time the tests took together, as the sum of their durations."""
    return _seconds(sum(row["duration"] for row in rows))


def _html(rows, stamp, summary) -> str:
    body = []
    for row in rows:
        body.append(
            "<tr class='{cls}'><td><code>{name}</code><div class='module'>{module}</div></td>"
            "<td>{description}</td><td>{expected}</td><td>{outcome}</td><td class='status'>{status}</td>"
            "<td class='duration'>{duration}</td></tr>".format(
                cls=row["status"].lower(), name=html.escape(row["name"]), module=html.escape(row["module"]),
                description=html.escape(row["description"]), expected=html.escape(row["expected"]),
                outcome=html.escape(row["outcome"]), status=row["status"], duration=_seconds(row["duration"])))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>End-to-end report</title>
<style>
:root {{ --bg: #fbfbfa; --fg: #1d1d1b; --muted: #6b6b66; --line: #dcdcd6; --pass: #1f7a3d; --fail: #b3261e;
        --skip: #8a6d00; }}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{ --bg: #161615; --fg: #ecece8; --muted: #9a9a94; --line: #3a3a37;
        --pass: #6fcf8a; --fail: #ff8a80; --skip: #e6c35c; }}
}}
:root[data-theme="dark"] {{ --bg: #161615; --fg: #ecece8; --muted: #9a9a94; --line: #3a3a37; --pass: #6fcf8a;
        --fail: #ff8a80; --skip: #e6c35c; }}
body {{ background: var(--bg); color: var(--fg); font: 14px/1.45 system-ui, sans-serif; margin: 0; padding: 16px; }}
h1 {{ font-size: 20px; margin: 0 0 4px; }}
.summary {{ color: var(--muted); margin-bottom: 16px; }}
table {{ width: 100%; border-collapse: collapse; }}
th, td {{ text-align: left; vertical-align: top; padding: 8px; border-bottom: 1px solid var(--line); }}
th {{ color: var(--muted); font-weight: 600; }}
code {{ font-size: 13px; }}
.module {{ color: var(--muted); font-size: 12px; }}
.status {{ font-weight: 700; white-space: nowrap; }}
.duration {{ color: var(--muted); white-space: nowrap; font-variant-numeric: tabular-nums; }}
tr.passed .status {{ color: var(--pass); }}
tr.failed .status, tr.error .status {{ color: var(--fail); }}
tr.skipped .status {{ color: var(--skip); }}
@media (max-width: 720px) {{ table, thead, tbody, tr, td {{ display: block; }} th {{ display: none; }}
  td {{ border: 0; padding: 2px 0; }} tr {{ border-bottom: 1px solid var(--line); padding: 8px 0; }} }}
</style></head>
<body>
<h1>End-to-end report</h1>
<div class="summary">{html.escape(stamp)} &middot; {len(rows)} tests: {html.escape(summary)} &middot; total {_total(rows)}</div>
<table>
<thead><tr><th>Test</th><th>Description</th><th>Expected behaviour</th><th>Outcome</th><th>Pass/fail</th><th>Duration</th></tr></thead>
<tbody>
{chr(10).join(body)}
</tbody></table>
</body></html>
"""
