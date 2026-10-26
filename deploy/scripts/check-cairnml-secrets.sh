#!/usr/bin/env bash
# Fails if a CairnML server secret, a private key or an .env file is committed to one of the CairnML
# repositories. It never prints a secret value: findings name the file and the secret's name only.
#
# Usage: check-cairnml-secrets.sh [repo ...]
# Defaults: this repository, its submodules (cairnml, cairnml-agent) and the web app (web/, see scripts/fetch-web.sh),
# and the secrets in $CAIRNML_HOME/secrets/cairnml (default ~/.local/share/cairnml).
set -uo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
secrets_dir="${CAIRNML_SECRETS_DIR:-${CAIRNML_HOME:-$HOME/.local/share/cairnml}/secrets/cairnml}"
repos=("$@")
if [ "${#repos[@]}" -eq 0 ]; then
  repos=("$root" "$root/cairnml" "$root/cairnml-agent" "$root/web")
fi

status=0
found() { echo "FOUND: $1"; status=1; }

for repo in "${repos[@]}"; do
  [ -e "$repo/.git" ] || { echo "skipped (not a git checkout): $repo"; continue; }

  # 1. The workspace's real secret values must not appear in any committed file.
  if [ -d "$secrets_dir" ]; then
    for file in "$secrets_dir"/*; do
      [ -f "$file" ] || continue
      value=$(head -c 4096 "$file" | tr -d '\n')
      [ "${#value}" -ge 16 ] || continue
      if git -C "$repo" grep -q -F -e "$value" -- . 2>/dev/null; then
        found "$repo: the value of secret '$(basename "$file")' is committed"
      fi
    done
  fi

  # 2. No private key material in tracked files.
  if git -C "$repo" grep -q -E -e "BEGIN (RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY" -- . 2>/dev/null; then
    found "$repo: a private key is committed"
  fi

  # 3. No .env file is tracked.
  if git -C "$repo" ls-files | grep -q -E '(^|/)\.env$'; then
    found "$repo: an .env file is tracked"
  fi

  # 4. A JSON "secret" value written in plain text (realm exports, client files).
  if git -C "$repo" grep -q -E -e '"secret"[[:space:]]*:[[:space:]]*"[^"$][^"]*"' -- . 2>/dev/null; then
    found "$repo: a plain-text \"secret\" value is committed (see the names with: git grep -n '\"secret\"')"
  fi
done

[ "$status" -eq 0 ] && echo "no secrets found in the checked repositories"
exit "$status"
