# -*- coding: utf-8 -*-
"""Mirror the CI validation guards against a local clash.yaml."""
import collections
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from airport import paths  # noqa: E402

# Default target is the generated file at the repo root, not one relative to
# the caller's working directory.
path = Path(sys.argv[1]) if len(sys.argv) > 1 else paths.OUTPUT_PATH
data = yaml.safe_load(path.read_bytes())
assert isinstance(data, dict), "not a YAML mapping"
for key in ("proxies", "proxy-groups", "rules"):
    assert data.get(key), f"missing/empty {key}"

groups = {g["name"]: g for g in data["proxy-groups"]}
required = {"AUTO-FAST": "url-test", "HK-POOL": "url-test", "JP-POOL": "url-test",
            "US-POOL": "url-test", "AI-POOL": "url-test", "FALLBACK": "fallback",
            "PROXY": "select"}
for name, typ in required.items():
    assert name in groups, f"missing group {name}"
    assert groups[name]["type"] == typ, f"{name} wrong type"
    assert groups[name]["proxies"], f"{name} empty"

for rule in ("DOMAIN-SUFFIX,openai.com,AI-POOL", "DOMAIN-SUFFIX,chatgpt.com,AI-POOL",
             "DOMAIN-SUFFIX,claude.ai,AI-POOL", "DOMAIN-SUFFIX,anthropic.com,AI-POOL",
             "GEOIP,CN,DIRECT", "MATCH,PROXY"):
    assert rule in data["rules"], f"missing rule {rule}"

for name, g in groups.items():
    if g["type"] in ("url-test", "fallback"):
        url = str(g.get("url", ""))
        assert "gstatic" not in url and "google.com" not in url, f"{name} CN-blackholed URL {url}"
        assert "cp.cloudflare.com" in url, f"{name} unexpected URL {url}"
    if g["type"] == "url-test":
        assert g.get("tolerance", 0) > 0, f"{name} missing tolerance"
        assert g.get("timeout", 0) >= 3000, f"{name} missing timeout"
assert "DIRECT" in groups["PROXY"]["proxies"], "PROXY lost DIRECT"

ends = [(str(p.get("server", "")).strip().lower(), p.get("port"))
        for p in data["proxies"]]
dup = len(ends) - len(set(ends))
assert dup == 0, f"{dup} duplicate endpoints"

import ipaddress
bad = []
for p in data["proxies"]:
    s = str(p.get("server", "")).strip().strip("[]")
    if s.lower() in ("localhost", "localhost.localdomain", "::1"):
        bad.append(s)
        continue
    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        continue
    if (ip.is_loopback or ip.is_unspecified or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_private):
        bad.append(s)
assert not bad, f"non-routable server addresses published: {bad[:5]}"
print(f"OK {path}: {len(ends)} nodes, {len(set(ends))} unique endpoints, "
      f"health URL {groups['AUTO-FAST']['url']}, generated-at {data['generated-at']}")
print("ALL VALIDATION PASSED")
