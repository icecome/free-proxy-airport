"""Merge discovery results into one reviewable candidate list.

Reads whatever discovery stages have produced and prints (and writes) a ranked
list of subscription URLs that are NOT yet in sources.txt, ready for a human to
copy over or for --append to add in one shot.

Usage:
    python scripts/report_candidates.py
    python scripts/report_candidates.py --append   # write into sources.txt
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from airport import paths  # noqa: E402

KNOWN = set()
try:
    with open(paths.SOURCES_FILE, encoding="utf-8") as _fh:
        for _line in _fh:
            _line = _line.strip()
            if _line and not _line.startswith("#"):
                KNOWN.add(_line)
except OSError:
    pass


def load(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--append", action="store_true")
    ap.add_argument("--min-tcp-pct", type=float, default=30.0)
    ap.add_argument("--min-nodes", type=int, default=20)
    args = ap.parse_args()

    checked = load(paths.DATA_DIR / "checked_github.json") or {"results": []}
    telegram = load(paths.DATA_DIR / "harvested_telegram.json") or {}
    disc_tg = load(paths.DATA_DIR / "discovered_telegram.json") or {}

    rows = []
    for r in checked.get("results", []):
        if not r.get("ok"):
            continue
        n = r.get("nodes", 0)
        pct = 100.0 * r.get("tcp_alive", 0) / max(1, r.get("tcp_sample", 1))
        rows.append(
            {
                "url": r["url"],
                "source": "github",
                "nodes": n,
                "tcp_pct": pct,
                "new": r["url"] not in KNOWN,
            }
        )

    # Telegram: only subscription URLs that actually served nodes
    for s in telegram.get("subscriptions", []):
        if s.get("nodes", 0) >= 5:
            rows.append(
                {
                    "url": s["url"],
                    "source": f"telegram/@{s['channel']}",
                    "nodes": s["nodes"],
                    "tcp_pct": 0.0,
                    "new": s["url"] not in KNOWN,
                }
            )

    rows.sort(key=lambda x: (-x["nodes"], -x["tcp_pct"]))
    fresh = [r for r in rows if r["new"]]

    print(f"discovered {len(rows)} live subscriptions, {len(fresh)} not in sources.txt\n")
    print(f"{'nodes':>6} {'tcp%':>5}  {'source':<26} url")
    print("-" * 110)
    for r in rows:
        mark = "+" if r["new"] else " "
        print(f"{r['nodes']:>6} {r['tcp_pct']:>5.0f}  {mark}{r['source']:<25} {r['url']}")

    picked = [
        r
        for r in fresh
        if r["tcp_pct"] >= args.min_tcp_pct and r["nodes"] >= args.min_nodes
    ]
    print(
        f"\nrecommended for sources.txt "
        f"(tcp>={args.min_tcp_pct:.0f}%, nodes>={args.min_nodes}): {len(picked)}"
    )
    for r in picked:
        print(f"  {r['url']}")

    if args.append and picked:
        with open(paths.SOURCES_FILE, "a", encoding="utf-8") as fh:
            fh.write(
                f"\n# === {time.strftime('%Y-%m-%d')} 自动发现 "
                f"(TCP 抽样存活>={args.min_tcp_pct:.0f}%，节点数>={args.min_nodes}) ===\n"
            )
            for r in picked:
                fh.write(f"{r['url']}\n")
        print(f"\nappended {len(picked)} URLs to sources.txt")

    with open(paths.DATA_DIR / "candidates_report.json", "w", encoding="utf-8") as fh:
        json.dump({"generated_at": int(time.time()), "rows": rows,
                   "recommended": picked}, fh, ensure_ascii=False, indent=2)
    print("\nwrote data/candidates_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
