#!/usr/bin/env bash
# Every pin (docker/build/*-ref) must be a commit that is on the main branch of its repository,
# so a pin never points at work that was not merged.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

declare -A repo=([sdk]=cairnml [agent]=cairnml-agent [web]=cairnml-web)
status=0
for pin in docker/build/*-ref; do
  key=$(basename "$pin" -ref)
  slug="gianlucapargaetzi/${repo[$key]}"
  sha=$(cat "$pin")
  state=$(gh api "repos/$slug/compare/main...$sha" --jq .status)
  case $state in
    identical|behind) echo "ok   $pin $sha is on main of $slug" ;;
    *) echo "FAIL $pin $sha is not on main of $slug ($state)"; status=1 ;;
  esac
done
exit $status
