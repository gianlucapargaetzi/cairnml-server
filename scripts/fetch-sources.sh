#!/usr/bin/env bash
# Puts the repositories this one builds from in folders next to it, each at the commit its pin file names:
#   SDK_REF (default main)    -> cairnml/        the SDK commit
#   AGENT_REF (default main)  -> cairnml-agent/  the agent commit
#   docker/build/web-ref     -> web/            the web app
# A pin file holds either a commit (40 hex characters), which is fetched exactly, or a branch name such as "main", which
# means the branch's latest commit at the time of the run. The web app tracks "main". The pin files are the record of
# which versions go together. Run this again after a pin changes, or after the branch has moved.
#
# To build the web app from a checkout of your own, for example to work on it before it is merged, set CAIRNML_WEB_DIR to
# its path. The compose files then use that folder, and web/ is left alone.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

fetch() { # fetch <folder> <repository> <commit or branch>
  local folder=$1 repo=$2 ref=$3 commit
  if [[ "$ref" =~ ^[0-9a-f]{40}$ ]]; then
    commit=$ref
  else
    commit=$(git ls-remote "https://github.com/gianlucapargaetzi/$repo.git" "refs/heads/$ref" | cut -f1)
    if [ -z "$commit" ]; then
      echo "$repo has no branch $ref" >&2
      exit 1
    fi
  fi
  if [ -d "$folder/.git" ] && [ "$(git -C "$folder" rev-parse HEAD)" = "$commit" ]; then
    echo "$folder/ is at $commit"
    return
  fi
  rm -rf "$folder"
  git clone --quiet "https://github.com/gianlucapargaetzi/$repo.git" "$folder"
  git -C "$folder" checkout --quiet "$commit"
  echo "$folder/ is at $commit"
}

fetch cairnml cairnml "${SDK_REF:-main}"
fetch cairnml-agent cairnml-agent "${AGENT_REF:-main}"
if [ -n "${CAIRNML_WEB_DIR:-}" ]; then
  echo "web/ is not fetched: the web app is in $CAIRNML_WEB_DIR"
else
  fetch web cairnml-web "$(cat docker/build/web-ref)"
fi
