#!/usr/bin/env python3
"""Merge verification reports into `data/node_registry.json` (verified / blacklist).

Verdict precedence is chosen by *which engine the subscriber actually runs*:
this project publishes a Clash/mihomo YAML, so mihomo's verdict is
authoritative. sing-box is a second opinion used to explain WHY a node failed
and to catch nodes mihomo rejects for engine-specific reasons.

Rules
  OK / SLOW in the mihomo pass        -> record_verified (24h TTL)
  anything else                        -> record_failed   (IP 7d / domain 3d)
  sing-box says OK but mihomo says no  -> still failed, detail marks the
                                          engine mismatch so the next reader
                                          can tell "dead" from "Clash can't
                                          use it".

Usage:
    python scripts/apply_verdicts.py                       # dry run
    python scripts/apply_verdicts.py --apply
    python scripts/apply_verdicts.py --apply --extra out/rescue_report.json
"""
from __future__ import annotations

# Allow `python airport/<module>.py` as well as `python -m airport.<module>`:
# the script form puts airport/ on sys.path, not the repo root.
if __package__ in (None, ""):
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import yaml

from airport import node_registry as registry
from airport import paths

ROOT = paths.ROOT


def load_sub(path: Path) -> dict[str, dict]:
    data = yaml.safe_load(path.read_bytes().decode("utf-8", "replace"))
    proxies = data.get("proxies", data) if isinstance(data, dict) else data
    return {registry.node_key(p): p for p in proxies if isinstance(p, dict) and p.get("server")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--primary", default=str(paths.VERIFY_REPORT),
                    help="mihomo pass report (authoritative)")
    ap.add_argument("--extra", action="append", default=[],
                    help="reports used only as evidence (rescue / cross-check)")
    ap.add_argument("--extra-authoritative", action="append", default=[],
                    help="additional mihomo reports whose OK verdicts also count as verified")
    ap.add_argument("--sub", default=str(paths.PAGES_PATH))
    ap.add_argument("--registry", default=str(paths.REGISTRY_FILE))
    ap.add_argument("--apply", action="store_true", help="write the registry (default: dry run)")
    args = ap.parse_args()

    sub = load_sub(Path(args.sub))
    primary = json.loads(Path(args.primary).read_text(encoding="utf-8"))

    # evidence maps: key -> short verdict from the secondary engine
    evidence: dict[str, str] = {}
    for path in args.extra:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        for row in doc.get("results", []):
            if row.get("ok"):
                evidence[row.get("key", "")] = "sing-box OK"
            else:
                evidence.setdefault(row.get("key", ""), f"sing-box {row.get('stage')}")

    # A node re-tested with mihomo in a follow-up run and found usable must be
    # verified, not blacklisted: the blacklist lasts 7 days for an IP.
    later_ok: dict[str, dict] = {}
    for path in args.extra_authoritative:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        for row in doc.get("results", []):
            if row.get("ok"):
                later_ok[row.get("key", "")] = row

    verified, failed, missing = [], [], 0
    for row in primary.get("results", []):
        key = row.get("key", "")
        proxy = sub.get(key)
        if proxy is None:
            missing += 1
            continue
        if not row.get("ok") and key in later_ok:
            row = later_ok[key]
        if row.get("ok"):
            verified.append((proxy, f"{row.get('stage')} {row.get('delay')}ms "
                                    f"{row.get('speed')}Mbps exit={row.get('exit_cc') or '?'}"))
        else:
            note = evidence.get(key, "")
            detail = row.get("stage", "UNKNOWN")
            if note == "sing-box OK":
                detail = f"{detail} (engine-mismatch: sing-box OK)"
            failed.append((proxy, detail))

    print(f"primary report: {args.primary}")
    print(f"  verified (mihomo OK/SLOW): {len(verified)}")
    print(f"  blacklisted              : {len(failed)}")
    print(f"  unmatched (not in sub)   : {missing}")
    print("\nverified nodes:")
    for proxy, detail in verified:
        print(f"  {detail:<42} {proxy.get('type'):<8} {proxy.get('server')}:{proxy.get('port')}")
    mismatch = [d for _, d in failed if "engine-mismatch" in d]
    if mismatch:
        print(f"\nengine-mismatch (sing-box OK, mihomo rejects): {len(mismatch)}")

    if not args.apply:
        print("\n[DRY-RUN] rerun with --apply to write the registry")
        return 0

    reg_path = Path(args.registry)
    if reg_path.exists():
        backup = reg_path.with_name(f"_node_registry-{time.strftime('%Y%m%d-%H%M%S')}.json")
        shutil.copy2(reg_path, backup)
        print(f"\n[INFO] registry backup -> {backup.name}")
    reg = registry.load_registry(reg_path)
    before = (len(reg["verified"]), len(reg["failed"]))
    for proxy, detail in verified:
        registry.record_verified(reg, proxy, detail)
    for proxy, detail in failed:
        registry.record_failed(reg, proxy, detail)
    reg = registry.prune(reg)
    registry.save_registry(reg, reg_path)
    print(f"[INFO] registry verified {before[0]} -> {len(reg['verified'])}  "
          f"failed(blacklist) {before[1]} -> {len(reg['failed'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
