#!/bin/sh
cd /workspace
git config user.name "nas-probe[bot]"
git config user.email "nas-probe@users.noreply.github.com"
git config --global --add safe.directory /workspace
cp /workspace/data/diag-out.txt /workspace/data/diag-pushed.txt
git add data/diag-pushed.txt
git commit -m "diag: NAS probe diagnostics [skip ci]"
git push origin HEAD:main
echo "PUSH_EXIT=$?"