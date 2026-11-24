#!/usr/bin/env python3
"""Materialize `data/node_registry.json` into the published subscription.

`generator.py` builds a subscription from a fresh cloud collection. This script
does the other half: when the published list has already been verified locally
and most of it is dead, rebuild the subscription from the registry's `verified`
entries only, so clients stop loading ~400 mostly-dead rows while the next
cloud run catches up.

It reuses the generator's own publish path (`build_config` / `validate_config`
/ `write_config`) rather than hand-writing YAML, so the output keeps every
group, rule and marker the CI validator expects.

Rules
  * credential-level dedupe: one row per (type, uuid|password). Two endpoints
    sharing a credential are the same upstream service; publishing both just
    doubles the dead weight. The faster copy wins.
  * endpoint-level dedupe: (host, port), enforced again by the generator.
  * non-routable servers are dropped by the generator's last-line filter.

Usage:
    python scripts/publish_verified.py                 # dry run (report only)
    python scripts/publish_verified.py --apply         # write output/ + docs/
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import generator as g  # noqa: E402
import node_registry as registry  # noqa: E402


# Protocols whose credential identifies the upstream *service*, so the same
# UUID/password on different CDN edges is one node published N times.
CREDENTIAL_IDENTIFIES_SERVICE = {"vmess", "vless", "trojan", "ss", "shadowsocks",
                                 "hysteria", "hysteria2", "tuic", "anytls"}
# A shared password is NOT identity for these: many unrelated SOCKS5/HTTP
# servers ship the same default ("s" / "Og==" seen 6x in this run), and
# collapsing them would delete working, distinct exits.
ENDPOINT_IDENTIFIES_SERVICE = {"socks5", "socks", "http", "https"}


def credential_key(proxy: dict) -> tuple[str, str]:
    proto = str(proxy.get("type", "")).lower()
    cred = str(proxy.get("uuid") or proxy.get("password") or "")
    endpoint = f"__{proxy.get('server')}:{proxy.get('port')}"
    if proto in ENDPOINT_IDENTIFIES_SERVICE or proto not in CREDENTIAL_IDENTIFIES_SERVICE:
        return (proto, endpoint)
    if not cred:
        return (proto, endpoint)
    return (proto, cred)


def parse_detail(detail: str) -> tuple[float, int]:
    """Pull (speed_mbps, delay_ms) back out of a registry detail string."""
    speed, delay = 0.0, 0
    for token in str(detail or "").split():
        try:
            if token.endswith("Mbps"):
                speed = float(token[:-4])
            elif token.endswith("ms"):
                delay = int(token[:-2])
        except ValueError:
            continue
    return speed, delay


def build_metrics() -> tuple[list[g.ProxyMetric], list[str]]:
    reg = registry.load_registry()
    now = time.time()
    rows, dropped = [], []
    for key, entry in reg["verified"].items():
        if now - entry.get("at", 0) >= registry.VERIFIED_TTL:
            continue
        proxy = entry.get("proxy")
        if not isinstance(proxy, dict) or not proxy.get("server"):
            continue
        speed, delay = parse_detail(entry.get("detail", ""))
        rows.append((credential_key(proxy), proxy, speed, delay))

    best: dict[tuple[str, str], tuple[dict, float, int]] = {}
    for cred, proxy, speed, delay in rows:
        prev = best.get(cred)
        if prev is None or (speed, -delay) > (prev[1], -prev[2]):
            if prev is not None:
                dropped.append(f"{prev[0].get('server')}:{prev[0].get('port')} "
                               f"(duplicate credential of {proxy.get('server')}:{proxy.get('port')})")
            best[cred] = (proxy, speed, delay)
        else:
            dropped.append(f"{proxy.get('server')}:{proxy.get('port')} "
                           f"(duplicate credential of {prev[0].get('server')}:{prev[0].get('port')})")

    metrics: list[g.ProxyMetric] = []
    for proxy, speed, delay in best.values():
        clean = {k: v for k, v in proxy.items() if not str(k).startswith("_")}
        name = str(clean.get("name", ""))
        for prefix in g.MARKER_PREFIXES:
            if name.startswith(prefix):
                name = name[len(prefix):]
                break
        clean["name"] = name
        metric = g.ProxyMetric(
            proxy=clean,
            latency=max(1, delay),
            region=g.detect_region(name),
            health_score=g.health_score(name, max(1, delay), g.detect_region(name), speed),
            speed_mbps=speed,
            url_test_ok=True,
        )
        metric.proxy["_verified_ok"] = True
        metrics.append(metric)
    return metrics, dropped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write output/clash.yaml + docs/clash.yaml")
    ap.add_argument("--min-speed", type=float, default=0.0,
                    help="drop nodes slower than this (Mbps); default keeps all verified")
    args = ap.parse_args()

    metrics, dropped = build_metrics()
    if args.min_speed > 0:
        before = len(metrics)
        metrics = [m for m in metrics if m.speed_mbps >= args.min_speed]
        print(f"[INFO] --min-speed {args.min_speed}: {before} -> {len(metrics)}")

    print(f"verified entries usable: {len(metrics)}")
    if dropped:
        print(f"credential dedupe dropped {len(dropped)}:")
        for line in dropped:
            print(f"  - {line}")
    print("\nfinal publish set (best first):")
    for m in sorted(metrics, key=g.publish_sort_key):
        print(f"  {m.speed_mbps:>6.2f}Mbps {m.latency:>5}ms {m.region:<5} "
              f"{m.proxy.get('type'):<8} {m.proxy.get('server')}:{m.proxy.get('port')}")

    if not metrics:
        print("\n[ABORT] nothing verified; refusing to publish an empty subscription")
        return 1
    if not args.apply:
        print("\n[DRY-RUN] rerun with --apply to write output/clash.yaml and docs/clash.yaml")
        return 0

    # R4: the published files are the product; snapshot them before overwrite.
    backup_dir = ROOT / f"_bak-{time.strftime('%Y%m%d')}"
    backup_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%H%M%S")
    for rel in ("output/clash.yaml", "docs/clash.yaml"):
        src = ROOT / rel
        if src.exists():
            dst = backup_dir / f"{src.parent.name}-clash-{stamp}.yaml"
            shutil.copy2(src, dst)
            print(f"[INFO] backup {rel} -> {dst.relative_to(ROOT)}")

    g.publish(metrics, len(metrics), len(metrics))
    docs = ROOT / "docs" / "clash.yaml"
    docs.write_bytes((ROOT / "output" / "clash.yaml").read_bytes())
    print(f"[INFO] docs/clash.yaml synced ({docs.stat().st_size} bytes)")

    data = yaml.safe_load(docs.read_bytes().decode("utf-8"))
    ends = [(str(p.get("server", "")).lower(), p.get("port")) for p in data["proxies"]]
    print(f"[CHECK] proxies={len(data['proxies'])} unique_endpoints={len(set(ends))} "
          f"groups={len(data['proxy-groups'])}")
    assert len(ends) == len(set(ends)), "duplicate endpoints after publish"
    return 0


if __name__ == "__main__":
    sys.exit(main())
