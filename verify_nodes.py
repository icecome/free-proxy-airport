#!/usr/bin/env python3
"""Full-stack node verifier.

The only test that answers "can I actually use this node?" is: build a real
proxy tunnel through it, fetch a page, and check that the exit IP is NOT ours.

Stages per node (short-circuits on first failure, each stage is recorded so we
can see WHERE the pipeline leaks):
  1. TCP        - raw connect to server:port
  2. TLS        - for TLS-bearing protocols, complete a TLS handshake
  3. TUNNEL     - sing-box builds a SOCKS port bound to this node only
  4. EGRESS     - GET http://ip-api.com/json via that SOCKS port, read country
  5. VERDICT    - egress country differs from local country => USABLE

Usage:
    python verify_nodes.py --sub <clash.yaml url or local path> [--limit N]
    python verify_nodes.py --sub-all        # every node from every source

Outputs:
    verify_report.json   per-node stage results + verdict
    verify_alive.txt     names of usable nodes, best first
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import random
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import node_registry as registry

HERE = Path(__file__).resolve().parent
SING_BOX = next(
    (p for p in HERE.glob(".tools/sing-box-*/sing-box.exe")), None
)
EGRESS_URL = "http://ip-api.com/json/?fields=status,country,countryCode,query"
TLS_TIMEOUT = 6.0
EGRESS_TIMEOUT = 12.0
TUNNEL_BOOT_TIMEOUT = 12.0


# --------------------------------------------------------------------------- #
# stage 1 & 2: TCP + TLS
# --------------------------------------------------------------------------- #
def probe_tcp(host: str, port: int, timeout: float = 5.0) -> tuple[bool, float]:
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, (time.monotonic() - t0) * 1000
    except OSError:
        return False, 0.0


def probe_tls(host: str, port: int, sni: str | None, timeout: float = TLS_TIMEOUT) -> tuple[bool, str]:
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=sni or host) as s:
                return True, s.version() or "?"
    except Exception as exc:
        return False, type(exc).__name__


def needs_tls(proxy: dict) -> bool:
    if proxy.get("type") in ("trojan", "hysteria", "hysteria2", "tuic", "anytls"):
        return True
    if proxy.get("tls") is True:
        return True
    if "reality-opts" in proxy:
        return True
    return False


# --------------------------------------------------------------------------- #
# stage 3-5: sing-box tunnel + real egress check
# --------------------------------------------------------------------------- #
# sing-box outbound schema is a STRICT whitelist: unknown keys abort config
# decode, and several fields are renamed vs mihomo (port -> server_port,
# cipher -> method, alterId -> alter_id ...). Build from known keys only.
ALLOWED_COMMON = {"type", "tag", "server", "server_port"}


def build_singbox_config(proxy: dict, port: int) -> dict:
    """Translate one mihomo-style proxy into a sing-box outbound."""
    proto = str(proxy.get("type", "")).lower()
    proto = {"socks5": "socks", "http": "http"}.get(proto, proto)

    out: dict = {"type": proto, "tag": "node", "server": str(proxy.get("server", ""))}
    try:
        out["server_port"] = int(proxy.get("port"))
    except (TypeError, ValueError):
        out["server_port"] = 0

    tls_on = bool(proxy.get("tls")) or proto in ("trojan", "hysteria", "hysteria2", "tuic") \
        or "reality-opts" in proxy
    sni = proxy.get("sni") or proxy.get("servername")
    fp = proxy.get("client-fingerprint")

    if proto in ("vmess", "vless"):
        out["uuid"] = str(proxy.get("uuid", ""))
        if proto == "vmess":
            out["alter_id"] = int(proxy.get("alterId") or 0)
            out["security"] = str(proxy.get("cipher") or "auto")
        if proto == "vless" and proxy.get("flow"):
            out["flow"] = str(proxy["flow"])
    elif proto in ("trojan", "hysteria2", "anytls"):
        out["password"] = str(proxy.get("password", ""))
    elif proto == "shadowsocks" or proto == "ss":
        out["method"] = str(proxy.get("cipher") or proxy.get("method") or "")
        out["password"] = str(proxy.get("password", ""))
    elif proto == "socks":
        if proxy.get("username"):
            out["username"] = str(proxy["username"])
        if proxy.get("password"):
            out["password"] = str(proxy["password"])
    elif proto == "hysteria":
        out["auth_str"] = str(proxy.get("password") or proxy.get("auth") or "")
        if proxy.get("up"):
            out["up"] = str(proxy["up"])
        if proxy.get("down"):
            out["down"] = str(proxy["down"])

    # transport (ws / grpc / http)
    net = proxy.get("network")
    ws = proxy.get("ws-opts") or {}
    grpc = proxy.get("grpc-opts") or {}
    if net == "ws" or ws:
        transport = {"type": "ws", "path": str(ws.get("path") or "/")}
        host = (ws.get("headers") or {}).get("Host")
        if host:
            transport["headers"] = {"Host": str(host)}
        out["transport"] = transport
    elif net == "grpc" or grpc:
        out["transport"] = {"type": "grpc",
                            "service_name": str(grpc.get("grpc-service-name") or "")}
    elif net in ("http", "h2"):
        out["transport"] = {"type": "http"}

    # TLS / REALITY
    reality = proxy.get("reality-opts")
    if tls_on or reality:
        tls_cfg: dict = {"enabled": True}
        if sni:
            tls_cfg["server_name"] = str(sni)
        if fp:
            tls_cfg["utls"] = {"enabled": True, "fingerprint": str(fp)}
        if reality:
            tls_cfg["reality"] = {
                "enabled": True,
                "public_key": str(reality.get("public-key", "")),
                "short_id": str(reality.get("short-id", "") or ""),
            }
        out["tls"] = tls_cfg

    return {
        "log": {"level": "error"},
        "inbounds": [{"type": "socks", "tag": "in", "listen": "127.0.0.1",
                       "listen_port": port}],
        "outbounds": [out],
    }


def get_egress(socks_port: int) -> tuple[bool, str, str]:
    """Fetch EGRESS_URL through the SOCKS tunnel. Returns (ok, country, ip).

    urllib cannot speak socks (unknown url type: socks5h), so requests+PySocks
    is used here. The system proxy is explicitly bypassed: neko-box switches it
    on and it would otherwise swallow the request before it reaches the tunnel.
    """
    import requests

    proxies = {
        "http": f"socks5h://127.0.0.1:{socks_port}",
        "https": f"socks5h://127.0.0.1:{socks_port}",
    }
    try:
        resp = requests.get(
            EGRESS_URL,
            proxies=proxies,
            timeout=EGRESS_TIMEOUT,
            headers={"User-Agent": "verify-nodes/1.0"},
        )
        data = resp.json()
    except Exception:
        return False, "", ""
    if data.get("status") != "success":
        return False, "", ""
    return True, data.get("countryCode", ""), data.get("query", "")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tunnel_probe(proxy: dict, local_country: str) -> dict:
    """Run sing-box for this single node and test real egress."""
    if SING_BOX is None or not SING_BOX.exists():
        return {"ok": False, "stage": "TUNNEL", "detail": "sing-box binary missing"}

    port = free_port()
    with tempfile.TemporaryDirectory(prefix="verify-") as td:
        cfg_path = Path(td) / "c.json"
        cfg_path.write_text(
            json.dumps(build_singbox_config(proxy, port)), encoding="utf-8"
        )
        proc = subprocess.Popen(
            [str(SING_BOX), "run", "-c", str(cfg_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            # wait for the SOCKS port
            deadline = time.monotonic() + TUNNEL_BOOT_TIMEOUT
            ready = False
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                        ready = True
                        break
                except OSError:
                    time.sleep(0.15)
            if not ready:
                err = b""
                try:
                    err = proc.stderr.read()[:400] if proc.stderr else b""
                except Exception:
                    pass
                return {
                    "ok": False,
                    "stage": "TUNNEL",
                    "detail": err.decode("utf-8", "replace").strip()[:200] or "sing-box failed to start",
                }

            ok, country, ip = get_egress(port)
            if not ok:
                return {"ok": False, "stage": "EGRESS", "detail": "no response via tunnel"}
            if country and country == local_country:
                return {"ok": False, "stage": "EGRESS", "detail": f"egress={country} (same as local, not proxied)"}
            return {"ok": True, "stage": "OK", "detail": f"exit={country} ip={ip}"}
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=4)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #
def local_country() -> str:
    """Our real country, measured with the system proxy bypassed.

    urllib reads the Windows system proxy, and neko-box switches that on while
    it runs, so a naive call reports the node's country instead of ours and
    every node then looks "not proxied".
    """
    import requests

    try:
        session = requests.Session()
        session.trust_env = False  # ignore the Windows/neko-box system proxy
        resp = session.get(EGRESS_URL, timeout=10, headers={"User-Agent": "verify-nodes/1.0"})
        return resp.json().get("countryCode", "CN")
    except Exception:
        return "CN"


def load_proxies(source: str) -> list[dict]:
    import yaml

    if source.startswith("http"):
        import urllib.request

        raw = urllib.request.urlopen(source, timeout=30).read()
    else:
        raw = Path(source).read_bytes()
    data = yaml.safe_load(raw.decode("utf-8", "replace"))
    if isinstance(data, dict):
        return [p for p in data.get("proxies", []) if isinstance(p, dict) and p.get("server")]
    return [p for p in data if isinstance(p, dict) and p.get("server")]


def verify(proxy: dict, local_cc: str) -> dict:
    host = str(proxy.get("server", ""))
    try:
        port = int(proxy.get("port", 0))
    except (TypeError, ValueError):
        return {"name": proxy.get("name", "?"), "stage": "PARSE", "ok": False}

    result = {"name": proxy.get("name", "?"), "type": proxy.get("type"),
              "server": host, "port": port, "tcp": False, "tls": None, "stage": "", "ok": False,
              "_key": registry.node_key(proxy)}

    tcp_ok, tcp_ms = probe_tcp(host, port)
    result["tcp"] = tcp_ok
    result["tcp_ms"] = round(tcp_ms)
    if not tcp_ok:
        result.update(stage="TCP", detail="tcp connect failed")
        return result

    if needs_tls(proxy):
        sni = proxy.get("sni") or proxy.get("servername")
        tls_ok, tls_info = probe_tls(host, port, sni)
        result["tls"] = tls_ok
        if not tls_ok:
            result.update(stage="TLS", detail=tls_info)
            return result
    else:
        result["tls"] = None

    tun = tunnel_probe(proxy, local_cc)
    result.update(stage=tun["stage"], ok=tun["ok"], detail=tun.get("detail", ""))
    return result


def select_incremental(proxies: list[dict], reg: dict) -> list[dict]:
    """Only nodes that need work: never seen, or verdict expired."""
    todo = []
    fresh_verified = 0
    blacklisted = 0
    for p in proxies:
        if registry.is_verified(reg, p):
            fresh_verified += 1
            continue
        if registry.is_blacklisted(reg, p):
            blacklisted += 1
            continue
        todo.append(p)
    print(
        f"incremental selection: skip_verified={fresh_verified} "
        f"skip_blacklisted={blacklisted} to_verify={len(todo)}"
    )
    return todo


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sub", default="https://icecome.github.io/free-proxy-airport/clash.yaml")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", default="verify_report.json")
    ap.add_argument("--full", action="store_true",
                    help="ignore the registry and re-verify everything")
    ap.add_argument("--registry", default=None, help="override registry path")
    args = ap.parse_args()

    reg_path = Path(args.registry) if args.registry else None
    reg = registry.load_registry(reg_path)
    proxies = load_proxies(args.sub)
    if args.limit:
        proxies = proxies[: args.limit]
    if not args.full:
        proxies = select_incremental(proxies, reg)
        if not proxies:
            print("nothing to verify; registry is current")
            registry.prune(reg)
            registry.save_registry(reg, reg_path)
            return 0
    cc = local_country()
    print(f"local country = {cc}, verifying {len(proxies)} nodes, workers={args.workers}")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(verify, p, cc): p for p in proxies}
        done = 0
        for f in concurrent.futures.as_completed(futs):
            results.append(f.result())
            done += 1
            if done % 25 == 0 or done == len(futs):
                ok = sum(1 for r in results if r["ok"])
                print(f"[{done}/{len(futs)}] usable={ok}")

    # stage histogram => shows exactly where the pipeline leaks
    from collections import Counter
    stages = Counter(r["stage"] for r in results)
    usable = [r for r in results if r["ok"]]

    # persist verdicts with expiry so the next run is incremental
    by_key = {}
    for p in proxies:
        by_key[registry.node_key(p)] = p
    for r in results:
        proxy = by_key.get(r.get("_key"))
        if not proxy:
            continue
        if r.get("ok"):
            registry.record_verified(reg, proxy, r.get("detail", ""))
        else:
            registry.record_failed(reg, proxy, r.get("stage", ""))
    reg = registry.prune(reg)
    registry.save_registry(reg, reg_path)
    print(f"registry updated: verified={len(reg['verified'])} failed={len(reg['failed'])}")

    Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    alive_names = [r["name"] for r in usable]
    Path("verify_alive.txt").write_text("\n".join(alive_names), encoding="utf-8")

    print("\n=== STAGE BREAKDOWN (where nodes die) ===")
    for stage, n in stages.most_common():
        print(f"  {stage:<8} {n:>5}")
    print(f"\nUSABLE: {len(usable)}/{len(results)} ({100*len(usable)//max(1,len(results))}%)")
    print(f"report -> {args.out}   names -> verify_alive.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
