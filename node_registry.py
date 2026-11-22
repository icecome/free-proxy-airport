"""Node registry: time-bound whitelist/blacklist shared by the cloud run and
the local verifier.

Free nodes are not stable: a node that works today may lose its credential or
get blocked tomorrow. So every verdict carries an expiry, and nothing is ever
permanent.

    VERIFIED   everything in `verified`  -> valid for VERIFIED_TTL (24h)
    FAILED     everything in `failed`     -> IP 7d / domain 3d
    UNKNOWN    absent from both           -> candidate for verification

Key format is `type|server|port|cred` so a different credential on the same
endpoint is treated as a different node (16/2208 endpoints behaved that way in
the full verification, so this matters, but the key is narrow enough that a
credential rotation is not permanently blacklisted).
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

REGISTRY_FILE = Path("data/node_registry.json")

VERIFIED_TTL = 24 * 3600        # a "working" node must be re-verified daily
FAILED_TTL_IP = 7 * 86400       # IP: gateway blocks may lift
FAILED_TTL_DOMAIN = 3 * 86400   # domain: DNS can re-point to a healthy edge


def is_ip(host: str) -> bool:
    return bool(re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", str(host)))


def node_key(proxy: dict) -> str:
    """Stable identity for a node: protocol + endpoint + credential."""
    cred = proxy.get("uuid") or proxy.get("password") or ""
    raw = "|".join([
        str(proxy.get("type", "")).lower(),
        str(proxy.get("server", "")).lower(),
        str(proxy.get("port", "")),
        str(cred),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def load_registry(path: Path | None = None) -> dict:
    p = path or REGISTRY_FILE
    if not p.exists():
        return {"verified": {}, "failed": {}, "updated_at": 0}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        data.setdefault("verified", {})
        data.setdefault("failed", {})
        return data
    except Exception:
        return {"verified": {}, "failed": {}, "updated_at": 0}


def save_registry(reg: dict, path: Path | None = None) -> None:
    p = path or REGISTRY_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(json.dumps(reg, ensure_ascii=False, indent=1).encode("utf-8"))


def record_verified(reg: dict, proxy: dict, detail: str = "", now: float | None = None) -> None:
    import time
    ts = now if now is not None else time.time()
    reg["verified"][node_key(proxy)] = {
        "server": str(proxy.get("server", "")),
        "port": proxy.get("port"),
        "type": proxy.get("type"),
        "at": int(ts),
        "detail": detail[:120],
        # keep the full config so publishing never has to re-match against
        # upstream sources (a source may drop the node while it still works)
        "proxy": {k: v for k, v in proxy.items() if not k.startswith("_")},
    }
    # a passing node is no longer failed
    reg["failed"].pop(node_key(proxy), None)


def record_failed(reg: dict, proxy: dict, stage: str = "", now: float | None = None) -> None:
    import time
    ts = now if now is not None else time.time()
    host = str(proxy.get("server", ""))
    ttl = FAILED_TTL_IP if is_ip(host) else FAILED_TTL_DOMAIN
    key = node_key(proxy)
    prev = reg["failed"].get(key, {})
    reg["failed"][key] = {
        "server": host,
        "port": proxy.get("port"),
        "type": proxy.get("type"),
        "stage": stage,
        "at": int(ts),
        # keep the original expiry if it is still longer-lived
        "expires_at": int(max(prev.get("expires_at", 0), ts + ttl)),
    }
    reg["verified"].pop(key, None)


def is_verified(reg: dict, proxy: dict, now: float | None = None) -> bool:
    import time
    ts = now if now is not None else time.time()
    entry = reg["verified"].get(node_key(proxy))
    return bool(entry and ts - entry.get("at", 0) < VERIFIED_TTL)


def is_blacklisted(reg: dict, proxy: dict, now: float | None = None) -> bool:
    import time
    ts = now if now is not None else time.time()
    entry = reg["failed"].get(node_key(proxy))
    if not entry:
        return False
    return ts < entry.get("expires_at", 0)


def prune(reg: dict, now: float | None = None) -> dict:
    """Drop expired entries so the file does not grow without bound."""
    import time
    ts = now if now is not None else time.time()
    reg["verified"] = {
        k: v for k, v in reg["verified"].items()
        if ts - v.get("at", 0) < VERIFIED_TTL
    }
    reg["failed"] = {
        k: v for k, v in reg["failed"].items()
        if ts < v.get("expires_at", 0)
    }
    reg["updated_at"] = int(ts)
    return reg


def expires_failed_row(entry: dict) -> bool:
    return bool(entry.get("expires_at"))
