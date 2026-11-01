#!/usr/bin/env python3
"""NAS 端中国视角拨测器（自动闭环，无人工参与）。

循环逻辑（由容器/cron 每 N 分钟触发一次运行）：
  1. git pull 领取云端任务（data/probe_queue.json）
  2. 队列无变化 → 退出（省资源）
  3. 对全部待测节点做 TCP 直连拨测（中国宽带视角）
  4. 结果写 data/cn_alive.json → git commit + push 回传

只依赖 Python 3.8+ 标准库（urllib 代替 requests，NAS 无需装包）。
"""
from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
QUEUE_FILE = REPO_DIR / "data" / "probe_queue.json"
RESULT_FILE = REPO_DIR / "data" / "cn_alive.json"
STATE_FILE = REPO_DIR / "data" / ".probe_state"

TIMEOUT_S = 4.0
WORKERS = 48
RETRIES = 2
RESULT_VALID_SECONDS = 3600  # 结果 1 小时内有效


def sh(args: list[str], cwd: Path | None = None) -> tuple[int, str]:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=120)
    return r.returncode, (r.stdout + r.stderr)


def git_pull() -> bool:
    code, out = sh(["git", "pull", "--rebase", "--autostash"])
    print(f"[git] pull: {out.strip().splitlines()[-1] if out.strip() else 'ok'}")
    return True


def git_push() -> bool:
    c1, _ = sh(["git", "add", "data/cn_alive.json"])
    c2, out = sh(["git", "diff", "--cached", "--quiet"])
    if c2 == 0:
        print("[git] result unchanged, nothing to push")
        return True
    c3, out = sh(["git", "commit", "-m", "probe: update CN alive results [skip ci]"])
    if c3 != 0:
        print(f"[git] commit failed: {out[:300]}")
        return False
    c4, out = sh(["git", "push"])
    if c4 != 0:
        print(f"[git] push failed: {out[:300]}")
        # 拉取重放，避免下次卡死
        sh(["git", "pull", "--rebase", "--autostash"])
        return False
    print("[git] results pushed")
    return True


def fingerprint(server: str, port: int, kind: str, cred: str) -> str:
    payload = json.dumps({"s": server, "p": port, "t": kind, "c": cred}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def tcp_alive(host: str, port: int, timeout: float) -> tuple[bool, float | None]:
    for attempt in range(RETRIES + 1):
        start = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True, (time.monotonic() - start) * 1000
        except OSError:
            if attempt < RETRIES:
                time.sleep(0.3)
    return False, None


def load_queue() -> list[dict]:
    if not QUEUE_FILE.exists():
        return []
    try:
        data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        return data.get("targets", []) if isinstance(data, dict) else []
    except Exception as exc:
        print(f"[WARN] queue parse failed: {exc}")
        return []


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def probe_all(targets: list[dict]) -> list[dict]:
    results = []

    def one(t: dict) -> dict:
        host = str(t.get("server", "")).strip()
        try:
            port = int(t.get("port", 0))
        except (TypeError, ValueError):
            port = 0
        base = {"fp": t.get("fp", ""), "server": host, "port": port, "alive": False, "tcp_ms": None}
        if not host or port <= 0 or port > 65535:
            base["error"] = "invalid"
            return base
        alive, ms = tcp_alive(host, port, TIMEOUT_S)
        base.update({"alive": alive, "tcp_ms": round(ms) if ms else None})
        return base

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for r in ex.map(one, targets):
            results.append(r)
    return results


def main() -> int:
    git_pull()
    queue = load_queue()
    if not queue:
        print("[INFO] empty queue, nothing to probe")
        return 0

    state = load_state()
    queue_sig = hashlib.sha256(json.dumps(queue, sort_keys=True).encode()).hexdigest()
    if state.get("last_queue_sig") == queue_sig and RESULT_FILE.exists():
        age = time.time() - RESULT_FILE.stat().st_mtime
        if age < RESULT_VALID_SECONDS / 2:
            print(f"[INFO] queue unchanged and results fresh ({int(age)}s old), skip")
            return 0

    print(f"[INFO] probing {len(queue)} targets from CN vantage...")
    t0 = time.monotonic()
    results = probe_all(queue)
    alive = [r for r in results if r["alive"]]
    print(f"[RESULT] alive={len(alive)}/{len(results)} in {time.monotonic() - t0:.1f}s")

    RESULT_FILE.parent.mkdir(parents=True, exist_ok=True)
    RESULT_FILE.write_text(
        json.dumps(
            {
                "probed_at": int(time.time()),
                "vantage": "nas-cn",
                "total": len(results),
                "alive_count": len(alive),
                "alive": alive,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    STATE_FILE.write_text(json.dumps({"last_queue_sig": queue_sig}))

    git_push()
    return 0


if __name__ == "__main__":
    sys.exit(main())
