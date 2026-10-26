#!/usr/bin/env bash
# Removes the Docker networks that no container uses. Networks in use are kept, and so are the built-in
# bridge, host and none networks. Run it by hand after a failed or interrupted test run.
set -euo pipefail

echo "Networks with no container attached (these are removed):"
for id in $(docker network ls -q); do
  name=$(docker network inspect -f '{{.Name}}' "$id")
  case "$name" in bridge|host|none) continue ;; esac
  containers=$(docker network inspect -f '{{len .Containers}}' "$id")
  if [ "$containers" = "0" ]; then echo "  $name"; fi
done

docker network prune -f
echo "Networks left:"
docker network ls --format '  {{.Name}}'
