# -*- coding: utf-8 -*-
"""Regression: non-routable server addresses are rejected at every ingress."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from airport import generator as g

# --- 1. is_non_routable_server covers 127.0.0.0/8 and friends ---
should_reject = [
    "127.0.0.53", "127.0.0.1", "127.1.2.3", "127.255.255.255",
    "::1", "[::1]", "0.0.0.0", "::", "169.254.1.1",
    "224.0.0.1", "240.0.0.1", "10.0.0.1", "192.168.1.1",
    "172.16.0.1", "localhost", "LOCALHOST", "localhost.localdomain",
    "ip6-localhost", "ip6-loopback", "", None,
]
should_keep = [
    "8.8.8.8", "1.1.1.1", "216.167.127.197", "122.10.119.252",
    "adheres-scruple.quick-cdn.net", "cp.cloudflare.com",
    "2001:4860:4860::8888", "example.com",
]
for s in should_reject:
    assert g.is_non_routable_server(s), f"MISSED reject: {s!r}"
for s in should_keep:
    assert not g.is_non_routable_server(s), f"FALSE reject: {s!r}"
print(f"is_non_routable_server: {len(should_reject)} rejected / "
      f"{len(should_keep)} kept -> OK")

# --- 2. normalize_proxy drops the exact node found in the published config ---
dirty = {
    "name": "[NoMoreWalls#001] 防范境外势力渗透",
    "type": "vmess", "server": "127.0.0.53", "port": 80,
    "uuid": "88888888-8888-8888-8888-888888888888",
    "alterId": 0, "cipher": "auto", "network": "tcp", "tls": False,
}
assert g.normalize_proxy(dirty, 1) is None, "loopback vmess survived normalize_proxy"
good = dict(dirty, server="8.8.8.8")
assert g.normalize_proxy(good, 1) is not None, "public node wrongly dropped"
print("normalize_proxy: loopback vmess dropped, public vmess kept -> OK")

# --- 3. sanitize_and_deduplicate filters a mixed batch ---
batch = [dirty] + [
    dict(good, name=f"ok-{i}", server=f"1.2.3.{i}") for i in range(1, 6)
]
out = g.sanitize_and_deduplicate(batch)
assert len(out) == 5, f"expected 5 survivors, got {len(out)}"
assert all(x["server"] != "127.0.0.53" for x in out)
print(f"sanitize_and_deduplicate: {len(batch)} -> {len(out)} -> OK")

# --- 4. load_existing_metrics bypass is closed ---
import tempfile, yaml, os
with tempfile.TemporaryDirectory() as d:
    cfg = {"proxies": [dirty, good]}
    p = Path(d) / "clash.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    original = g.OUTPUT_PATH
    g.OUTPUT_PATH = p
    try:
        metrics = g.load_existing_metrics()
    finally:
        g.OUTPUT_PATH = original
    assert len(metrics) == 1, f"legacy bypass leaked loopback: {len(metrics)}"
    print("load_existing_metrics: loopback filtered on republish path -> OK")

# --- 5. publish() last-line defence ---
import json
with tempfile.TemporaryDirectory() as d:
    outp = Path(d) / "clash.yaml"
    original = g.OUTPUT_PATH
    g.OUTPUT_PATH = outp
    try:
        ms = [
            g.ProxyMetric(proxy=dict(dirty, name="loop"), latency=100,
                          region="OTHER", health_score=1.0),
            g.ProxyMetric(proxy=dict(good, name="pub"), latency=100,
                          region="OTHER", health_score=0.9),
        ]
        g.publish(ms, 2, 2)
        published = yaml.safe_load(outp.read_bytes())["proxies"]
    finally:
        g.OUTPUT_PATH = original
    assert all(x["server"] != "127.0.0.53" for x in published), \
        "publish() wrote a loopback node"
    print("publish(): loopback dropped by last-line filter -> OK")

print("\nALL LOOPBACK CHECKS PASSED")
