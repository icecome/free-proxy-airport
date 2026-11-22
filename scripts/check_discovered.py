"""First-pass health check of discovered subscription URLs.

Only answers "can this URL still be fetched and does it still hold nodes",
plus a quick TCP reachability sample of the node endpoints. Full tunnel
verification belongs to verify_nodes.py, not here.

Usage:
    python scripts/check_discovered.py --in data/discovered_github.json \
        --out data/checked_github.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts._probe_lib import (  # noqa: E402
    b64_try_decode,
    count_nodes,
    dump_json,
    http_get_hard,
    tcp_alive,
)

PROXY_RE = re.compile(
    r"\b(?:vmess|vless|trojan|ss|ssr|hysteria2|tuic)://[^\s\"'<>\\]+", re.I
)


def yaml_endpoints(body: bytes, n: int) -> list[tuple[str, int]]:
    """host:port pairs from a Clash/mihomo proxies: list."""
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        return []
    text = b64_try_decode(body).decode("utf-8", "replace")
    try:
        data = yaml.safe_load(text)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(data, dict) or not isinstance(data.get("proxies"), list):
        return []
    out = []
    for p in data["proxies"]:
        if not isinstance(p, dict):
            continue
        host = p.get("server")
        try:
            port = int(p.get("port") or 0)
        except (TypeError, ValueError):
            continue
        if host and port:
            out.append((str(host), port))
        if len(out) >= n:
            break
    return out


def endpoint_sample(body: bytes, n: int = 12) -> list[tuple[str, int]]:
    """Pull a few host:port pairs out of the payload for a TCP sample."""
    y = yaml_endpoints(body, n)
    if y:
        return y
    text = b64_try_decode(body).decode("utf-8", "replace")
    out: list[tuple[str, int]] = []
    seen = set()
    for link in PROXY_RE.findall(text):
        scheme = link.split("://", 1)[0].lower()
        host = port = None
        if scheme == "vmess":
            import base64  # noqa: PLC0415
            import binascii  # noqa: PLC0415

            try:
                raw = link[8:]
                raw += "=" * (-len(raw) % 4)
                j = json.loads(base64.b64decode(raw))
                host, port = j.get("add"), int(j.get("port") or 0)
            except (binascii.Error, ValueError, TypeError, json.JSONDecodeError):
                continue
        else:
            m = re.match(r"[a-z0-9]+://[^@]*@([^:/?#]+):(\d+)", link, re.I)
            if m:
                host, port = m.group(1), int(m.group(2))
        if host and port and (host, port) not in seen:
            seen.add((host, port))
            out.append((host, port))
        if len(out) >= n:
            break
    return out


MIRROR = "http://ghproxy.net/"


def check(url: str, sample: int = 12) -> dict:
    # raw.githubusercontent.com is GFW-reset from CN, so try the mirror too.
    status, body, _ = http_get_hard(url, hard_timeout=20.0)
    if status != 200:
        status, body, _ = http_get_hard(MIRROR + url, hard_timeout=20.0)
    if status != 200:
        return {"url": url, "ok": False, "reason": f"HTTP {status}"}
    n, kinds = count_nodes(body)
    if n < 5:
        return {"url": url, "ok": False, "reason": f"only {n} nodes"}
    eps = endpoint_sample(body, sample)
    alive = sum(1 for h, p in eps if tcp_alive(h, p, timeout=4))
    return {
        "url": url,
        "ok": True,
        "nodes": n,
        "kinds": kinds,
        "bytes": len(body),
        "tcp_sample": len(eps),
        "tcp_alive": alive,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    ap.add_argument("--sample", type=int, default=12)
    args = ap.parse_args()

    src = json.load(open(args.inp, encoding="utf-8"))
    urls: list[str] = []
    label = {}
    for hit in src.get("hits", []):
        best = hit["subscriptions"][0]
        urls.append(best["canonical"])
        label[best["canonical"]] = hit["full_name"]

    print(f"checking {len(urls)} subscription URLs...")
    results = []
    for u in urls:
        r = check(u, args.sample)
        if r["ok"]:
            pct = 100.0 * r["tcp_alive"] / max(1, r["tcp_sample"])
            print(
                f"  OK   {label[u]:<42} nodes={r['nodes']:<5} "
                f"tcp {r['tcp_alive']}/{r['tcp_sample']} ({pct:.0f}%)"
            )
        else:
            print(f"  DEAD {label[u]:<42} {r['reason']}")
        results.append(r)

    good = [r for r in results if r["ok"]]
    good.sort(key=lambda r: -(r["tcp_alive"] / max(1, r["tcp_sample"])))
    dump_json(args.out, {"checked": len(results), "alive": len(good),
                         "results": results})
    print(f"\n{len(good)}/{len(results)} still serve nodes -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
