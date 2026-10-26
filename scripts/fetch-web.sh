#!/usr/bin/env bash
# Puts the web app at web/, at the commit that docker/build/web-ref names. That file is the one pin of the web app: the
# server image, the stack and the end-to-end suite all build from it. Run it again after the pin changes.
#
# To build from a checkout of your own, for example to work on the web app before it is merged, set CAIRNML_WEB_DIR to
# its path. The compose files then use that folder, and this script does nothing.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

if [ -n "${CAIRNML_WEB_DIR:-}" ]; then
  echo "using the web app in $CAIRNML_WEB_DIR"
  exit 0
fi

ref=$(cat docker/build/web-ref)
if [ -d web/.git ] && [ "$(git -C web rev-parse HEAD)" = "$ref" ]; then
  echo "web/ is at $ref"
  exit 0
fi

rm -rf web
git clone --quiet https://github.com/gianlucapargaetzi/cairnml-web.git web
git -C web checkout --quiet "$ref"
echo "web/ is at $ref"
