#!/usr/bin/env bash
# Applies the repository settings and branch rules to every CairnML repository, so they are code, not clicks.
# Safe to run again: it updates what exists. Needs an authenticated gh with admin rights on the repositories.
#
#   scripts/github-setup.sh            apply to all repositories
#   scripts/github-setup.sh cairnml    apply to one
#   DRY_RUN=1 scripts/github-setup.sh  print what would be sent
set -euo pipefail

OWNER=${OWNER:-gianlucapargaetzi}  # the account whose repositories are set up; set OWNER to use another
# external-datasets is not here on purpose: the server adds dataset entries to it through a pull request on a
# register/<name> branch that merges itself (auto-merge, merge commit) once its check passes. Its rules stay as they are.
ALL=(cairnml cairnml-agent cairnml-server cairnml-web cairnml-stack)
DRY_RUN=${DRY_RUN:-0}

# The checks a pull request must pass, by repository. A name is the job name, and the job must run on every pull request.
checks_for() {
  case "$1" in
    cairnml|cairnml-agent) echo test package gitleaks dependency-review codeql ;;
    cairnml-server)        echo test docker-build gitleaks dependency-review codeql ;;
    cairnml-web)           echo build gitleaks dependency-review codeql ;;
    cairnml-stack)         echo lint pins gitleaks dependency-review codeql ;;
  esac
}

api() {
  if [ "$DRY_RUN" = 1 ]; then echo "gh api $*"; else gh api "$@" --silent; fi
}

main_ruleset() {
  local checks_json
  checks_json=$(for c in $(checks_for "$1"); do printf '{"context":"%s"},' "$c"; done)
  checks_json="[${checks_json%,}]"
  cat <<JSON
{
  "name": "Protect main",
  "target": "branch",
  "enforcement": "active",
  "bypass_actors": [],
  "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "required_linear_history"},
    {"type": "pull_request", "parameters": {
      "required_approving_review_count": 0,
      "dismiss_stale_reviews_on_push": false,
      "require_code_owner_review": false,
      "require_last_push_approval": false,
      "required_review_thread_resolution": true,
      "allowed_merge_methods": ["squash"]
    }},
    {"type": "required_status_checks", "parameters": {
      "strict_required_status_checks_policy": true,
      "required_status_checks": $checks_json
    }}
  ]
}
JSON
}

branch_names_ruleset() {
  cat <<'JSON'
{
  "name": "Branch names",
  "target": "branch",
  "enforcement": "active",
  "bypass_actors": [],
  "conditions": {"ref_name": {"include": ["~ALL"], "exclude": ["~DEFAULT_BRANCH", "refs/heads/feature/**", "refs/heads/fix/**", "refs/heads/dependabot/**", "refs/heads/incoming/**"]}},
  "rules": [{"type": "creation"}]
}
JSON
}

release_tags_ruleset() {
  cat <<'JSON'
{
  "name": "Release tags",
  "target": "tag",
  "enforcement": "active",
  "bypass_actors": [],
  "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
  "rules": [{"type": "update"}, {"type": "deletion"}, {"type": "non_fast_forward"}]
}
JSON
}

put_ruleset() {  # repo, name, json
  local repo=$1 name=$2 json=$3 id
  id=$(gh api "repos/$OWNER/$repo/rulesets" --jq ".[] | select(.name==\"$name\") | .id" 2>/dev/null || true)
  if [ "$DRY_RUN" = 1 ]; then echo "ruleset '$name' on $repo ($([ -n "$id" ] && echo update || echo create))"; echo "$json"; return; fi
  if [ -n "$id" ]; then
    echo "$json" | gh api -X PUT "repos/$OWNER/$repo/rulesets/$id" --input - --silent
  else
    echo "$json" | gh api -X POST "repos/$OWNER/$repo/rulesets" --input - --silent
  fi
}

# A deployment environment holds a publishing credential or a trusted-publisher identity. It can be used only from the
# named refs (main, or v* tags), and a publish to a real registry waits for the owner's approval.
environment() {  # repo, name, refs (main|tags), reviewer (yes|no)
  local repo=$1 name=$2 refs=$3 reviewer=$4 reviewers='[]' policy_name policy_type body
  if [ "$refs" = tags ]; then policy_name='v*'; policy_type=tag; else policy_name=main; policy_type=branch; fi
  if [ "$reviewer" = yes ]; then
    local owner_id; owner_id=$([ "$DRY_RUN" = 1 ] && echo OWNER_ID || gh api "users/$OWNER" --jq .id)
    reviewers="[{\"type\":\"User\",\"id\":$owner_id}]"
  fi
  body="{\"reviewers\":$reviewers,\"deployment_branch_policy\":{\"protected_branches\":false,\"custom_branch_policies\":true}}"
  if [ "$DRY_RUN" = 1 ]; then echo "environment $name on $repo ($refs, reviewer=$reviewer): $body"; return; fi
  echo "$body" | gh api -X PUT "repos/$OWNER/$repo/environments/$name" --input - --silent
  local have; have=$(gh api "repos/$OWNER/$repo/environments/$name/deployment-branch-policies" --jq '.branch_policies[].name')
  for existing in $have; do
    [ "$existing" = "$policy_name" ] || gh api -X DELETE "repos/$OWNER/$repo/environments/$name/deployment-branch-policies/$(gh api "repos/$OWNER/$repo/environments/$name/deployment-branch-policies" --jq ".branch_policies[]|select(.name==\"$existing\")|.id")" --silent
  done
  if ! grep -qxF "$policy_name" <<<"$have"; then
    gh api -X POST "repos/$OWNER/$repo/environments/$name/deployment-branch-policies" -f "name=$policy_name" -f "type=$policy_type" --silent
  fi
}

apply() {
  local repo=$1
  echo "== $repo"
  # Merging: squash only, delete the branch afterwards. No auto-merge: every merge is done by hand.
  api -X PATCH "repos/$OWNER/$repo" \
    -F allow_squash_merge=true -F allow_merge_commit=false -F allow_rebase_merge=false \
    -F allow_auto_merge=false -F delete_branch_on_merge=true \
    -F has_wiki=false -F has_projects=false
  # Only GitHub's own actions plus the ones this project uses; the default token is read-only.
  api -X PUT "repos/$OWNER/$repo/actions/permissions" -F enabled=true -f allowed_actions=selected
  api -X PUT "repos/$OWNER/$repo/actions/permissions/selected-actions" \
    -F github_owned_allowed=true -F verified_allowed=false \
    -f 'patterns_allowed[]=pypa/gh-action-pypi-publish@*' -f 'patterns_allowed[]=pnpm/action-setup@*' -f 'patterns_allowed[]=github/codeql-action/*@*' \
    -f 'patterns_allowed[]=docker/*@*' -f 'patterns_allowed[]=aquasecurity/trivy-action@*' -f 'patterns_allowed[]=aquasecurity/setup-trivy@*'
  api -X PUT "repos/$OWNER/$repo/actions/permissions/workflow" -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false
  api -X PUT "repos/$OWNER/$repo/actions/permissions/fork-pr-contributor-approval" -f approval_policy=all_external_contributors
  api -X PATCH "repos/$OWNER/$repo" \
    -f 'security_and_analysis[secret_scanning][status]=enabled' \
    -f 'security_and_analysis[secret_scanning_push_protection][status]=enabled'
  api -X PUT "repos/$OWNER/$repo/private-vulnerability-reporting"
  api -X PUT "repos/$OWNER/$repo/vulnerability-alerts"
  put_ruleset "$repo" "Protect main" "$(main_ruleset "$repo")"
  put_ruleset "$repo" "Branch names" "$(branch_names_ruleset)"
  put_ruleset "$repo" "Release tags" "$(release_tags_ruleset)"
  case $repo in
    cairnml|cairnml-agent) environment "$repo" testpypi main no; environment "$repo" pypi tags yes ;;
    cairnml-server)        environment "$repo" dockerhub tags yes ;;
  esac
}

for repo in "${@:-${ALL[@]}}"; do apply "$repo"; done
