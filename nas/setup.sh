#!/bin/sh
exec > /workspace/setup.log 2>&1
echo "=== setup start ==="
date
apk add --no-cache git
cd /workspace
if [ ! -d /workspace/.git/refs ]; then
  git clone --depth 1 "https://x-access-token:${GH_TOKEN}@github.com/icecome/free-proxy-airport.git" /workspace/repo-tmp
  rm -rf /workspace/.git
  mv /workspace/repo-tmp/.git /workspace/.git
  rm -rf /workspace/repo-tmp
fi
git log --oneline -3
git config user.name "nas-probe[bot]"
git config user.email "nas-probe@users.noreply.github.com"
git config --global --add safe.directory /workspace
python3 /workspace/nas_probe.py
echo "probe exit code: $?"
echo "=== starting loop ==="
exec sh /workspace/loop.sh