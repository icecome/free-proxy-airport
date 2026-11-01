#!/bin/sh
cd /workspace
{
  echo "=== diag $(date) ==="
  ls -la /workspace 2>&1 | head -20
  echo "--- setup.log tail ---"
  tail -30 /workspace/setup.log 2>/dev/null || echo "(no setup.log)"
  echo "--- queue ---"
  cat /workspace/data/probe_queue.json 2>/dev/null | head -5 || echo "(no queue)"
  echo "--- live probe of first 15 ---"
  python3 -c "
import json, socket, time
from pathlib import Path
q = Path('/workspace/data/probe_queue.json')
print('queue exists:', q.exists())
if q.exists():
    d = json.loads(q.read_text())
    print('queued:', len(d.get('targets', [])))
    for t in d.get('targets', [])[:15]:
        s, p = t.get('server'), int(t.get('port', 0))
        st = time.monotonic()
        try:
            with socket.create_connection((s, p), timeout=4):
                print('ALIVE', s, p, int((time.monotonic()-st)*1000), 'ms')
        except Exception as e:
            print('DEAD ', s, p, type(e).__name__)
"
} > /workspace/data/diag-out.txt 2>&1
chmod 666 /workspace/data/diag-out.txt
echo done