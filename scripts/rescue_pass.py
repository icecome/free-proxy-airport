#!/usr/bin/env python3
"""Second-opinion rescue pass over the nodes the mihomo pass rejected.

A single engine's verdict is not a safe basis for a blacklist. Measured on a
55-node cross-check (2026-11-24): mihomo found 15 usable, sing-box 16, only 12
agreed — three nodes each way were "dead" to one engine and fine to the other
(sing-box additionally rejects REALITY nodes whose cert lacks an IP SAN, mihomo
accepts them; mihomo's delay API reports 503 for tunnels sing-box completes).

A blacklist entry is a 7-day (IP) / 3-day (domain) sentence in
`data/node_registry.json`, so every node the first pass rejected gets:

  1. TCP retry   - 3 attempts x 6s, because a single SYN drop is often noise
  2. sing-box    - independent tunnel, real egress (ip-api), then a streaming
                   speed probe through that tunnel

Nodes whose TCP connect fails all three attempts are NOT sent to sing-box: the
engine starts with the same socket connect, so it can only reproduce the
failure. That is recorded as `tcp-dead` rather than silently skipped.

Usage:
    python scripts/rescue_pass.py                       # uses out/verify_report.json
    python scripts/rescue_pass.py --workers 10
    python scripts/rescue_pass.py --limit 20 --report out/smoke_report.json

Output: out/rescue_report.json
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import node_registry as registry  # noqa: E402
import verify_nodes as vn  # noqa: E402  (reuses build_singbox_config)

EGRESS_URL = "http://ip-api.com/json/?fields=status,country,countryCode,query"
SPEED_URL = "https://speed.cloudflare.com/__down?bytes=4000000"
SPEED_MAX_BYTES = 3_000_000
SPEED_TIMEOUT = 10.0
TCP_ATTEMPTS = 3
TCP_TIMEOUT = 6.0
BOOT_TIMEOUT = 12.0
EGRESS_TIMEOUT = 10.0


def session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False  # never let the system proxy swallow the probe
    return s


HTTP = session()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def tcp_retry(proxy: dict) -> tuple[bool, int]:
    host = str(proxy.get("server", ""))
    try:
        port = int(proxy.get("port", 0))
    except (TypeError, ValueError):
        return False, 0
    if not host or not (0 < port < 65536):
        return False, 0
    best = 0
    for attempt in range(TCP_ATTEMPTS):
        start = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=TCP_TIMEOUT):
                return True, max(1, round((time.monotonic() - start) * 1000))
        except OSError:
            if attempt + 1 < TCP_ATTEMPTS:
                time.sleep(0.4)
    return False, best


def socks_egress(port: int) -> tuple[bool, str, str]:
    proxies = {"http": f"socks5h://127.0.0.1:{port}",
               "https": f"socks5h://127.0.0.1:{port}"}
    try:
        r = HTTP.get(EGRESS_URL, proxies=proxies, timeout=EGRESS_TIMEOUT,
                     headers={"User-Agent": "free-proxy-airport-rescue/1.0"})
        data = r.json()
    except Exception:  # noqa: BLE001
        return False, "", ""
    if data.get("status") != "success":
        return False, "", ""
    return True, str(data.get("countryCode", "")), str(data.get("query", ""))


def socks_speed(port: int) -> float:
    proxies = {"http": f"socks5h://127.0.0.1:{port}",
               "https": f"socks5h://127.0.0.1:{port}"}
    received = 0
    start = time.monotonic()
    try:
        with HTTP.get(SPEED_URL, stream=True, proxies=proxies, timeout=SPEED_TIMEOUT,
                      headers={"User-Agent": "free-proxy-airport-rescue/1.0"}) as resp:
            if resp.status_code != 200:
                return 0.0
            for chunk in resp.iter_content(chunk_size=65536):
                received += len(chunk)
                if received >= SPEED_MAX_BYTES:
                    break
    except Exception:  # noqa: BLE001
        return 0.0
    elapsed = time.monotonic() - start
    if elapsed < 0.4 or received < 200_000:
        return 0.0
    return received * 8 / elapsed / 1_000_000


def singbox_probe(proxy: dict, work_dir: Path, tag: str) -> dict:
    """Boot one sing-box for this node, measure egress + speed, tear down."""
    if vn.SING_BOX is None or not vn.SING_BOX.exists():
        return {"stage": "ENGINE", "ok": False, "detail": "sing-box binary missing"}
    port = free_port()
    cfg_path = work_dir / f"{tag}.json"
    cfg_path.write_text(json.dumps(vn.build_singbox_config(proxy, port)), encoding="utf-8")
    log_path = work_dir / f"{tag}.log"
    with log_path.open("wb") as err:
        proc = subprocess.Popen([str(vn.SING_BOX), "run", "-c", str(cfg_path)],
                                stdout=subprocess.DEVNULL, stderr=err)
        try:
            deadline = time.monotonic() + BOOT_TIMEOUT
            ready = False
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                        ready = True
                        break
                except OSError:
                    time.sleep(0.1)
            if not ready:
                tail = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-2:]
                return {"stage": "TUNNEL", "ok": False,
                        "detail": (" | ".join(tail) or "sing-box failed to start")[:200]}

            start = time.monotonic()
            got, cc, ip = socks_egress(port)
            delay = round((time.monotonic() - start) * 1000) if got else 0
            speed = socks_speed(port) if got else 0.0
            if not got:
                return {"stage": "EGRESS", "ok": False, "delay": 0,
                        "detail": "no response via tunnel"}
            return {"stage": "OK", "ok": True, "delay": delay,
                    "speed": round(speed, 2), "exit_cc": cc, "exit_ip": ip,
                    "detail": "" if speed > 0 else "egress ok, speed probe empty"}
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=4)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass


def rescue_one(proxy: dict, work_dir: Path, index: int) -> dict:
    row = {
        "key": registry.node_key(proxy),
        "name": str(proxy.get("name", "")),
        "type": str(proxy.get("type", "")),
        "server": str(proxy.get("server", "")),
        "port": proxy.get("port"),
    }
    ok, ms = tcp_retry(proxy)
    row["tcp_retry_ok"] = ok
    row["tcp_retry_ms"] = ms
    if not ok:
        row.update(stage="TCP", ok=False, detail=f"tcp dead after {TCP_ATTEMPTS} attempts")
        return row
    got = singbox_probe(proxy, work_dir, f"n{index}")
    row.update(stage=got["stage"], ok=bool(got.get("ok")),
               sb_delay=got.get("delay", 0), sb_speed=got.get("speed", 0.0),
               exit_cc=got.get("exit_cc", ""), exit_ip=got.get("exit_ip", ""),
               detail=got.get("detail", ""))
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default=str(ROOT / "out" / "verify_report.json"))
    ap.add_argument("--sub", default=str(ROOT / "docs" / "clash.yaml"))
    ap.add_argument("--out", default=str(ROOT / "out" / "rescue_report.json"))
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    data = yaml.safe_load(Path(args.sub).read_bytes().decode("utf-8", "replace"))
    by_key = {registry.node_key(p): p for p in data.get("proxies", []) if isinstance(p, dict)}

    rejected = [r for r in report.get("results", []) if not r.get("ok")]
    if args.limit:
        rejected = rejected[: args.limit]
    print(f"[INFO] first pass rejected {len(rejected)} nodes; retrying TCP x{TCP_ATTEMPTS}")

    work_dir = ROOT / "out" / "rescue"
    work_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    rows: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {}
        for i, r in enumerate(rejected):
            proxy = by_key.get(r.get("key"))
            if proxy is None:
                rows.append({"key": r.get("key"), "name": r.get("name"), "server": r.get("server"),
                             "port": r.get("port"), "stage": "MISSING", "ok": False,
                             "detail": "not found in subscription"})
                continue
            futs[pool.submit(rescue_one, proxy, work_dir, i)] = i
        for done, fut in enumerate(concurrent.futures.as_completed(futs), start=1):
            rows.append(fut.result())
            if done % 25 == 0 or done == len(futs):
                rescued = sum(1 for x in rows if x.get("ok"))
                print(f"[{done}/{len(futs)}] rescued={rescued} ({time.monotonic() - t0:.0f}s)")

    for row in rows:
        row["mihomo_stage"] = next(
            (r.get("stage") for r in report["results"] if r.get("key") == row.get("key")), "?")
    rescued = [r for r in rows if r.get("ok")]
    tcp_dead = [r for r in rows if r.get("stage") == "TCP"]
    tunnel_dead = [r for r in rows if not r.get("ok") and r.get("stage") != "TCP"]
    from collections import Counter
    print("\n=== RESCUE RESULT ===")
    print(f"  rescued (usable via sing-box) : {len(rescued)}")
    print(f"  tcp-dead (3 attempts)         : {len(tcp_dead)}")
    print(f"  tunnel/egress-dead            : {len(tunnel_dead)}")
    print(f"  stage histogram               : {dict(Counter(r['stage'] for r in rows))}")

    payload = {
        "generated_at": int(time.time()),
        "engine": str(vn.SING_BOX),
        "source_report": str(args.report),
        "tcp_attempts": TCP_ATTEMPTS,
        "counts": {"rejected": len(rejected), "rescued": len(rescued),
                   "tcp_dead": len(tcp_dead), "tunnel_dead": len(tunnel_dead)},
        "results": rows,
    }
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nreport -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
