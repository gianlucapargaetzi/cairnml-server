#!/usr/bin/env bash
# Builds wheels of the SDK and the agent from the working trees of their submodules, for a machine or an agent that is not
# this checkout:
#   pip install --find-links dist/dev cairnml cairnml-agent
# The version is the one in version.py with a local label: 0.0.1+dev.<commit>, and .dirty when the tree has changes. A
# local version sorts above the release it is built on and satisfies the server's >=0.0.1. These wheels are never
# uploaded: a release goes through TestPyPI first (the publish workflow of each package).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

out=dist/dev
rm -rf "$out"
mkdir -p "$out"

for pkg in cairnml cairnml-agent; do
  module=${pkg//-/_}
  label="dev.$(git -C "$pkg" rev-parse --short=8 HEAD)"
  if [ -n "$(git -C "$pkg" status --porcelain)" ]; then
    label="$label.dirty"
  fi

  # A copy of the working tree without build output: a stale file in build/ would end up in the wheel.
  build=$(mktemp -d)
  (cd "$pkg" && tar --exclude=./.git --exclude=./build --exclude=./dist --exclude='*.egg-info' \
    --exclude=__pycache__ --exclude=.pytest_cache -cf - .) | tar -C "$build" -xf -
  release=$(sed -nE "s/^__version__ = ['\"]([^'\"]+)['\"].*/\1/p" "$build/$module/version.py")
  sed -i -E "s/^__version__ = (['\"])[^'\"]+(['\"])/__version__ = \1$release+$label\2/" "$build/$module/version.py"

  python3 -m pip wheel --no-deps --wheel-dir "$out" "$build"
  rm -rf "$build"
done
ls "$out"
