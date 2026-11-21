"""Hand-written commits on the rolling branch ``chore/api-sync`` of the sync workflow.

Every run of the workflow rebuilds that branch from the consumer's ``main`` and force-pushes it, which would destroy a
commit somebody added to adapt the hand-written code. The decision whether such a commit is there is made here, not in
shell: a commit counts as hand-written when its author email is not the sync's own. When one is present the workflow
neither pushes nor closes the pull request; it comments once on the open pull request (the comment carries a marker,
and a run that finds the marker does not comment again) or notes it in the step summary.

The commit list is the output of ``git log --format='%H%x09%ae%x09%s' origin/main..origin/chore/api-sync`` (the commits
of the branch that are not on the consumer's ``main``); a list that cannot be read is refused, never read as "no manual
commits", because the answer decides whether work is overwritten.

Commands (the workflow passes files and a sha; nothing from a commit reaches shell text)::

    python -m apiserver.contract.manual_commits decide --commits-file F [--bot-email EMAIL]
                                       prints "manual <number of manual commits>" or "none"
    python -m apiserver.contract.manual_commits comment-body --commits-file F --sha SHA --out FILE
                                       writes the comment, which starts with the marker
    python -m apiserver.contract.manual_commits has-comment --comments-file F
                                       prints "yes" when a comment of the listing (the output of `gh api --paginate` on
                                       the comments endpoint of the pull request) has the marker, else "no"
"""

import argparse
import re
import sys
from pathlib import Path
from typing import List, NamedTuple

from apiserver.contract.breaking_issue import load_listing
from apiserver.contract.pr_body import SHA as SHA_PATTERN

BOT_EMAIL = "release@example.invalid"  # the author email the sync workflow sets for its commits
MARKER = "<!-- api-sync-manual-commits -->"
MAX_SHOWN = 10
MAX_SUBJECT = 100
COMMIT_ID = re.compile(r"^[0-9a-f]{40}$")


class Commit(NamedTuple):
    sha: str
    email: str
    subject: str


def parse_commits(text: str) -> List[Commit]:
    """The commits of ``git log --format='%H%x09%ae%x09%s'``: a full sha, the author email and the subject, separated by
    tabs. A blank line is skipped; any other line that is not of this form is an error."""
    commits = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        fields = line.rstrip("\r").split("\t", 2)
        if len(fields) < 2 or not COMMIT_ID.match(fields[0]) or not fields[1].strip():
            raise ValueError(f"line {number} of the commit list is not '<sha>\\t<author email>\\t<subject>'")
        commits.append(Commit(fields[0], fields[1].strip(), fields[2] if len(fields) > 2 else ""))
    return commits


def manual_commits(commits: List[Commit], bot_email: str = BOT_EMAIL) -> List[Commit]:
    """The commits whose author email is not the bot's (email addresses compare without regard to case)."""
    bot = bot_email.strip().lower()
    return [c for c in commits if c.email.lower() != bot]


def _plain(text: str) -> str:
    """One line of printable characters, short, without anything that markdown would read as code or a link."""
    line = "".join(ch for ch in " ".join(text.split()) if ch.isprintable())
    line = line.replace("`", "'").replace("<", "(").replace(">", ")")
    return line if len(line) <= MAX_SUBJECT else line[: MAX_SUBJECT - 3] + "..."


def build_comment(commits: List[Commit], sha: str, branch: str = "chore/api-sync") -> str:
    """The comment for the open pull request: what stands on the branch, and what the sync does about it."""
    if not isinstance(sha, str) or not SHA_PATTERN.match(sha):
        raise ValueError(f"not a full lower-case commit sha: {sha!r}")
    if not commits:
        raise ValueError("there is no manual commit to comment on")
    shown = [f"- `{c.sha[:8]}` {_plain(c.subject)}" for c in commits[:MAX_SHOWN]]
    if len(commits) > MAX_SHOWN:
        shown.append(f"- and {len(commits) - MAX_SHOWN} more")
    plural = "commit" if len(commits) == 1 else "commits"
    return (
        f"{MARKER}\n"
        f"The branch `{branch}` has {len(commits)} {plural} that the sync did not make:\n\n"
        + "\n".join(shown)
        + f"\n\nThe sync of cairnml-server@{sha[:8]} stands aside: it does not push to this branch and does not close "
        "this pull request, so these commits are not overwritten. It runs as before when the branch is merged or "
        "deleted, or holds only the sync's own commits again. This comment is made once.\n"
    )


def has_comment(comments: List[dict]) -> bool:
    """Whether a comment of the pull request already has the marker (a line of its own, so that quoting the text in a
    sentence does not count)."""
    for comment in comments:
        body = comment.get("body")
        if isinstance(body, str) and any(line.strip() == MARKER for line in body.splitlines()):
            return True
    return False


def _read(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"cannot read {path}: {error}") from error


def _decide(args) -> int:
    found = manual_commits(parse_commits(_read(args.commits_file)), args.bot_email)
    print(f"manual {len(found)}" if found else "none")
    return 0


def _comment_body(args) -> int:
    found = manual_commits(parse_commits(_read(args.commits_file)), args.bot_email)
    Path(args.out).write_text(build_comment(found, args.sha), encoding="utf-8")
    return 0


def _has_comment(args) -> int:
    print("yes" if has_comment(load_listing(args.comments_file, "comment listing")) else "no")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m apiserver.contract.manual_commits", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    decide = sub.add_parser("decide")
    decide.add_argument("--commits-file", required=True)
    decide.add_argument("--bot-email", default=BOT_EMAIL)
    decide.set_defaults(run=_decide)
    body = sub.add_parser("comment-body")
    body.add_argument("--commits-file", required=True)
    body.add_argument("--bot-email", default=BOT_EMAIL)
    body.add_argument("--sha", required=True)
    body.add_argument("--out", required=True)
    body.set_defaults(run=_comment_body)
    has = sub.add_parser("has-comment")
    has.add_argument("--comments-file", required=True)
    has.set_defaults(run=_has_comment)
    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
