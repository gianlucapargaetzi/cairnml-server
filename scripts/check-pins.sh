#!/usr/bin/env bash
# Every submodule must be pinned at a commit that is on the main branch of its repository,
# so a pin never points at work that was not merged.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
status=0
while read -r key path; do
  name=${key#submodule.}
  name=${name%.path}
  url=$(git config -f .gitmodules --get "submodule.$name.url")
  slug=$(echo "$url" | sed -E 's#^(git@github.com:|https://github.com/)##; s#\.git$##')
  sha=$(git ls-tree HEAD "$path" | awk '{print $3}')
  state=$(gh api "repos/$slug/compare/main...$sha" --jq .status)
  case $state in
    identical|behind) echo "ok   $path $sha is on main of $slug" ;;
    *) echo "FAIL $path $sha is not on main of $slug ($state)"; status=1 ;;
  esac
done < <(git config -f .gitmodules --get-regexp '^submodule\..*\.path$')
exit $status
