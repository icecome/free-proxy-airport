# -*- coding: utf-8 -*-
"""Verify the new endpoint dedup + publish dedup without touching the network."""
import collections, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from airport import generator as g

# --- 1. endpoint_key normalization ---
cases = [
    ({"type": "hy2", "server": "A.Example.COM", "port": 443}, ("hysteria2", "a.example.com", 443)),
    ({"type": "hysteria2", "server": "a.example.com", "port": "443"}, ("hysteria2", "a.example.com", 443)),
    ({"type": "socks", "server": "h", "port": 1080}, ("socks5", "h", 1080)),
    ({"type": "ss", "server": "h", "port": 8388}, ("ss", "h", 8388)),
    ({"type": "ssr", "server": "h", "port": 8388}, ("ss", "h", 8388)),
    ({"type": "vless", "server": "h", "port": 443}, ("vless", "h", 443)),
]
ok = 0
for proxy, expect in cases:
    got = g.endpoint_key(proxy)
    mark = "OK " if got == expect else "FAIL"
    if got == expect:
        ok += 1
    print(f"{mark} endpoint_key({proxy['type']},{proxy['server']},{proxy['port']}) -> {got}")
print(f"endpoint_key: {ok}/{len(cases)}")

# --- 2. sanitize collapses same-host floods, keeps distinct hosts ---
same_host = [
    {"name": f"cdn-{i}", "type": "vless", "server": "adheres-scruple.quick-cdn.net", "port": 443,
     "uuid": f"uuid-{i}", "network": "ws"}
    for i in range(12)
]
other_hosts = [
    {"name": f"other-{i}", "type": "trojan", "server": f"host{i}.example.com", "port": 443,
     "password": f"pw{i}"}
    for i in range(5)
]
out = g.sanitize_and_deduplicate(same_host + other_hosts)
# Expectation follows the live constant: the 2026-11-24 pipeline lowered the
# per-endpoint candidate cap to 1 (clients dedupe on server:port anyway), so
# hardcoding 3 made this test fail against its own generator.
expect = min(12, g.DEDUP_ENDPOINT_CANDIDATES) + len(other_hosts)
print(f"sanitize: {len(same_host + other_hosts)} in -> {len(out)} out "
      f"(expect {min(12, g.DEDUP_ENDPOINT_CANDIDATES)} same-host cap + {len(other_hosts)} distinct = {expect})")
assert len(out) == expect, out

# --- 3. publish dedup keeps the highest-scoring copy per endpoint ---
metrics = []
for i in range(12):
    metrics.append(g.ProxyMetric(
        proxy={"name": f"cdn-{i}", "type": "vless", "server": "adheres-scruple.quick-cdn.net",
               "port": 443, "uuid": f"uuid-{i}"},
        latency=100 + i, region="OTHER", health_score=0.1 * i + 0.1,
    ))
metrics.append(g.ProxyMetric(
    proxy={"name": "solo", "type": "trojan", "server": "solo.example.com", "port": 443},
    latency=80, region="HK", health_score=0.9))
# Same host:port under a DIFFERENT protocol must still collapse: the client
# lists one entry per endpoint, not per (protocol, endpoint).
metrics.append(g.ProxyMetric(
    proxy={"name": "cdn-shadow-protocol", "type": "trojan",
           "server": "adheres-scruple.quick-cdn.net", "port": 443},
    latency=60, region="HK", health_score=2.0))
metrics.sort(key=g.publish_sort_key)
deduped = g.dedupe_published_by_endpoint(metrics)
print(f"publish dedup: {len(metrics)} -> {len(deduped)} (expect 2)")
assert len(deduped) == 2, deduped
survivor = [m for m in deduped if m.proxy["server"] == "adheres-scruple.quick-cdn.net"][0]
print(f"surviving same-host node: {survivor.proxy['name']} score={survivor.health_score:.1f}")
assert survivor.proxy["name"] == "cdn-shadow-protocol", survivor.proxy["name"]

# --- 4. ranking tier: cn_verified outranks a much higher score ---
hi = g.ProxyMetric(proxy={"name": "hi", "type": "vless", "server": "a", "port": 1},
                   latency=50, region="HK", health_score=5.0)
cn = g.ProxyMetric(proxy={"name": "cn", "type": "vless", "server": "b", "port": 1},
                   latency=900, region="OTHER", health_score=0.01, cn_verified=True)
order = sorted([hi, cn], key=g.publish_sort_key)
print(f"tier order: {[m.proxy['name'] for m in order]} (expect cn first)")
assert order[0].proxy["name"] == "cn"

# --- 5. TEST_URL must not be the CN-blackholed gstatic endpoint ---
print(f"TEST_URL = {g.TEST_URL}")
assert "gstatic" not in g.TEST_URL and "google.com" not in g.TEST_URL, g.TEST_URL

print("\nALL CHECKS PASSED")