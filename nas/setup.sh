#!/bin/sh
set -e
apk add --no-cache git >/dev/null 2>&1
if [ ! -d /workspace/.git ]; then
  if [ -z "$GH_TOKEN" ]; then echo "[setup] GH_TOKEN missing, waiting"; exit 1; fi
  git clone "https://x-access-token:${GH_TOKEN}@github.com/icecome/free-proxy-airport.git" /workspace/repo-tmp
  mv /workspace/repo-tmp/.git /workspace/.git
  rm -rf /workspace/repo-tmp
fi
cd /workspace
git config user.name "nas-probe[bot]"
git config user.email "nas-probe@users.noreply.github.com"
git config --global --add safe.directory /workspace
exec sh /workspace/loop.sh