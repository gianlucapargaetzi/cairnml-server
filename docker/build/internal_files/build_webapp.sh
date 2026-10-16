#!/usr/bin/env bash
set -x
set -e

cd /opt/open-webapp/
corepack enable && corepack prepare pnpm@10 --activate
pnpm install --frozen-lockfile

cd /opt/open-webapp/
pnpm run build
pnpm run build-widgets
