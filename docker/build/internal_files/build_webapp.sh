#!/usr/bin/env bash
set -x
set -e

cd /opt/open-webapp/
# The build context may carry a local node_modules from the host; a clean install is required.
rm -rf node_modules
corepack enable && corepack prepare pnpm@10 --activate
pnpm install --frozen-lockfile

cd /opt/open-webapp/
pnpm run build
pnpm run build-widgets
