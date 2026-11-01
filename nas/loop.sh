#!/bin/sh
# NAS 拨测循环容器入口：每 INTERVAL 秒执行一次拨测
set -e
INTERVAL="${PROBE_INTERVAL:-900}"
echo "[probe] loop started, interval=${INTERVAL}s"
while true; do
  sleep "$INTERVAL" || true
  cd /workspace
  python3 /app/nas_probe.py 2>&1 || echo "[probe] run failed, will retry next interval"
done
