"""本地三网节点存活拨测（天津/河北视角）。

原理：对订阅里每个节点的 server:port 直接发起 TCP 连接。
连接成功 = 你的宽带能触达该节点入口 = 该节点对你的网络"存活"。
不经过代理、不需要测速端点、不需要科学上网（鸡蛋问题不存在）。

用法：
    python cn_alive_check.py                          # 测当前订阅
    python cn_alive_check.py -o alive.txt             # 输出存活节点名的 base64 订阅
    python cn_alive_check.py --timeout 3000           # 单节点超时 ms（默认 4000）

结果分级：
    直连成功      -> 节点入口对你的三网可达（可能仍被协议墙识别，但入口通）
    超时/拒绝     -> 节点对你不可用，客户端里必死，直接淘汰
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import socket
import sys
import time
from pathlib import Path

import requests
import yaml

SUB_URL = "https://icecome.github.io/free-proxy-airport/clash.yaml"
TIMEOUT_S = 4.0
WORKERS = 64
# TCP 直连失败重试次数（减少抖动误杀）
RETRIES = 2


def fix_mojibake(text: str) -> str:
    """Repair CJK double-encoding (UTF-8 bytes mis-decoded as Latin-1)."""
    try:
        text.encode("latin-1").decode("utf-8")
        return text
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    try:
        return text.encode("latin-1", errors="ignore").decode("utf-8", errors="ignore")
    except Exception:
        return text


def load_proxies(sub_url: str) -> list[dict]:
    resp = requests.get(sub_url, timeout=20, headers={"User-Agent": "cn-alive-check"})
    # Always decode from raw bytes as UTF-8; requests may guess ISO-8859-1 for
    # yaml responses without charset, producing mojibake before we ever parse.
    text = resp.content.decode("utf-8", errors="replace")
    data = yaml.safe_load(text)
    return [p for p in data.get("proxies", []) if isinstance(p, dict) and p.get("server")]


def tcp_alive(host: str, port: int, timeout: float) -> tuple[bool, float | None]:
    """True if TCP connect succeeds within timeout. Returns (alive, ms)."""
    for attempt in range(RETRIES + 1):
        start = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True, (time.monotonic() - start) * 1000
        except OSError:
            if attempt < RETRIES:
                time.sleep(0.3)
    return False, None


def check_one(proxy: dict, timeout: float) -> dict:
    host, port = str(proxy["server"]), int(proxy["port"])
    alive, ms = tcp_alive(host, port, timeout)
    return {"name": proxy.get("name", "?"), "type": proxy.get("type"), "server": host,
            "port": port, "alive": alive, "tcp_ms": round(ms) if ms else None}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sub", default=SUB_URL)
    ap.add_argument("--timeout", type=float, default=TIMEOUT_S)
    ap.add_argument("--output", "-o", default=None, help="write alive nodes as base64 subscription")
    ap.add_argument("--json", default=None, help="write full report json")
    args = ap.parse_args()

    proxies = load_proxies(args.sub)
    print(f"loaded {len(proxies)} nodes from subscription")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(check_one, p, args.timeout): p for p in proxies}
        done = 0
        for fut in concurrent.futures.as_completed(futs):
            results.append(fut.result())
            done += 1
            if done % 50 == 0 or done == len(futs):
                alive_now = sum(1 for r in results if r["alive"])
                print(f"[INFO] {done}/{len(futs)} checked, alive={alive_now}")

    alive = sorted([r for r in results if r["alive"]], key=lambda r: r["tcp_ms"])
    dead = [r for r in results if not r["alive"]]
    print(f"\n[RESULT] alive={len(alive)}/{len(results)} ({100*len(alive)//max(1,len(results))}%)")
    print("fastest 15:")
    for r in alive[:15]:
        print(f"  {r['tcp_ms']:>4}ms  {r['name'][:40]}  {r['server']}:{r['port']}")

    if args.json:
        Path(args.json).write_text(json.dumps({"alive": alive, "dead": dead}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"report -> {args.json}")

    if args.output:
        # fetch full proxy dicts for alive names, emit as clash yaml for direct use
        resp = requests.get(args.sub, timeout=20)
        data = yaml.safe_load(resp.content.decode("utf-8", errors="replace"))
        keep = {r["name"] for r in alive}
        alive_proxies = [p for p in data.get("proxies", []) if p.get("name") in keep]
        out = {"proxies": alive_proxies}
        Path(args.output).write_text(yaml.safe_dump(out, allow_unicode=True, sort_keys=False), encoding="utf-8")
        print(f"alive clash yaml -> {args.output}")


if __name__ == "__main__":
    main()
