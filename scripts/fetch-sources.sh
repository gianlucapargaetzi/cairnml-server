#!/usr/bin/env bash
# Puts the repositories this one builds from in folders next to it, each at the commit its pin file names:
#   docker/build/sdk-ref     -> cairnml/        the SDK
#   docker/build/agent-ref   -> cairnml-agent/  the agent
#   docker/build/web-ref     -> web/            the web app
# A pin file holds either a commit (40 hex characters), which is fetched exactly, or a branch name such as "main", which
# means the branch's latest commit at the time of the run. The web app tracks "main". The pin files are the record of
# which versions go together. Run this again after a pin changes, or after the branch has moved.
#
# To build the web app from a checkout of your own, for example to work on it before it is merged, set CAIRNML_WEB_DIR to
# its path. The compose files then use that folder, and web/ is left alone.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

fetch() { # fetch <folder> <repository> <pin file>
  local folder=$1 repo=$2 pin=$3 ref commit
  ref=$(cat "$pin")
  if [[ "$ref" =~ ^[0-9a-f]{40}$ ]]; then
    commit=$ref
  else
    commit=$(git ls-remote "https://github.com/gianlucapargaetzi/$repo.git" "refs/heads/$ref" | cut -f1)
    if [ -z "$commit" ]; then
      echo "$repo has no branch $ref (named in $pin)" >&2
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

fetch cairnml cairnml docker/build/sdk-ref
fetch cairnml-agent cairnml-agent docker/build/agent-ref
if [ -n "${CAIRNML_WEB_DIR:-}" ]; then
  echo "web/ is not fetched: the web app is in $CAIRNML_WEB_DIR"
else
  fetch web cairnml-web docker/build/web-ref
fi
