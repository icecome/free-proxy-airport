"""Discover public proxy subscription URLs on GitHub.

No token required (unauthenticated search is 10 req/min; we sleep between pages).
For each candidate repo we probe a matrix of well-known subscription paths and
keep the ones that actually return proxy data.

Usage:
    python scripts/discover_github.py --out data/discovered_github.json
    python scripts/discover_github.py --pages 5 --max-repos 150
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from airport import paths  # noqa: E402

from scripts._probe_lib import (  # noqa: E402
    count_nodes,
    dump_json,
    fetch_raw,
    http_get,
    parallel,
)

SEARCH_URL = "https://api.github.com/search/repositories"

QUERIES = [
    "免费节点",
    "免费机场",
    "免费订阅",
    "clash订阅",
    "v2ray订阅",
    "节点合集",
    "白嫖节点",
    "clash free nodes",
    "free v2ray subscription",
    "v2ray free nodes",
    "proxy pool clash",
    "免费梯子",
    "节点分享",
    "ssr subscription",
    "mihomo subscription",
]

# Paths that real subscription repos actually use. Kept short on purpose:
# probing 30 paths x 200 repos is ~6000 requests and burns the whole run.
# Order matters: the mirror is quick on paths that exist and can stall ~19s on
# paths that 404, so the highest-probability paths go first and a hit short
# circuits the rest.
CANDIDATE_PATHS = [
    "clash.yaml",
    "sub/v2ray.txt",
    "output/clash.yaml",
    "sub/sub.yaml",
    "sub/clash.yaml",
    "v2ray.txt",
    "sub.yaml",
    "clash.yml",
    # case variants matter: cbusifabcap publishes sub/V2Ray.yml, not
    # sub/v2ray.txt, and raw.githubusercontent paths are case sensitive
    "sub/V2Ray.yml",
    "sub/clash.yml",
    "Clash.yaml",
    "config.yaml",
    "meta.yaml",
    "subscribe/clash.yml",
]


def search_repos(query: str, page: int = 1, per_page: int = 50) -> list[dict]:
    qs = urllib.parse.urlencode(
        {
            "q": query,
            "sort": "updated",
            "order": "desc",
            "per_page": per_page,
            "page": page,
        }
    )
    url = f"{SEARCH_URL}?{qs}"
    headers = {"Accept": "application/vnd.github+json"}
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    status, body, _ = http_get(url, timeout=25, retries=1, headers=headers)
    if status != 200:
        print(f"  search FAIL {query!r} p{page}: HTTP {status} {body[:160]!r}")
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return []
    out = []
    for it in data.get("items", []):
        out.append(
            {
                "full_name": it.get("full_name"),
                "html_url": it.get("html_url"),
                "default_branch": it.get("default_branch") or "main",
                "pushed_at": it.get("pushed_at"),
                "stargazers": it.get("stargazers_count", 0),
                "size_kb": it.get("size", 0),
                "description": (it.get("description") or "")[:200],
            }
        )
    return out


def probe_repo(repo: dict, budget: float = 25.0) -> dict | None:
    """Probe a repo's candidate paths CONCURRENTLY.

    Serial probing died on ghproxy.net 404s (19s each), so a 8-path repo took
    3 minutes. Firing the path matrix in parallel bounds a repo at roughly one
    fetch latency instead of the sum of all misses.
    """
    name = repo["full_name"]
    branches = []
    for b in (repo.get("default_branch"), "main", "master"):
        if b and b not in branches:
            branches.append(b)

    jobs = [(br, path) for br in branches[:1] for path in CANDIDATE_PATHS]
    found: list[dict] = []

    def one(job):
        br, path = job
        status, body, used = fetch_raw(
            name, br, path, timeout=min(8.0, budget))
        if status != 200 or len(body) < 200:
            return None
        n, kinds = count_nodes(body)
        if n < 5:
            return None
        return {
            "url": used,
            "canonical": f"https://raw.githubusercontent.com/{name}/{br}/{path}",
            "nodes": n,
            "kinds": kinds,
            "bytes": len(body),
        }

    for _job, res in parallel(jobs, one, workers=min(8, len(jobs))):
        if isinstance(res, dict):
            found.append(res)

    if not found:
        return None
    # one subscription per repo: the biggest payload is the canonical feed
    found.sort(key=lambda x: -x["nodes"])
    repo = dict(repo)
    repo["subscriptions"] = found
    repo["best_nodes"] = found[0]["nodes"]
    return repo


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=2, help="search pages per query")
    ap.add_argument("--min-stars", type=int, default=0)
    ap.add_argument("--max-repos", type=int, default=400)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--sleep", type=float, default=7.0, help="seconds between search calls")
    ap.add_argument("--repo-budget", type=float, default=25.0,
                    help="seconds allowed per repo before giving up")
    ap.add_argument("--out", default=str(paths.DATA_DIR / "discovered_github.json"))
    args = ap.parse_args()

    print(f"[1/3] GitHub search: {len(QUERIES)} queries x {args.pages} pages")
    repos: dict[str, dict] = {}
    for qi, q in enumerate(QUERIES):
        for page in range(1, args.pages + 1):
            got = search_repos(q, page)
            new = 0
            for r in got:
                if not r.get("full_name"):
                    continue
                if r["stargazers"] < args.min_stars:
                    continue
                if r["full_name"] not in repos:
                    repos[r["full_name"]] = r
                    new += 1
            print(f"  {q!r} p{page}: {len(got)} items, +{new} new (total {len(repos)})")
            if not got:
                break
            if len(repos) >= args.max_repos:
                break
            time.sleep(args.sleep)
        if len(repos) >= args.max_repos:
            break

    todo = list(repos.values())[: args.max_repos]
    print(f"\n[2/3] Probing {len(todo)} repos for subscription paths...")

    def work(r):
        return probe_repo(r, budget=args.repo_budget)

    results = []
    done = 0
    for repo, res in parallel(todo, work, workers=args.workers):
        done += 1
        if isinstance(res, Exception):
            continue
        if res:
            results.append(res)
            print(
                f"  [{done}/{len(todo)}] HIT {res['full_name']} "
                f"nodes={res['best_nodes']} kinds={res['subscriptions'][0]['kinds']}"
            )
        elif done % 20 == 0:
            print(f"  ... {done}/{len(todo)}")

    results.sort(key=lambda x: -x["best_nodes"])

    known = set()
    try:
        with open(paths.SOURCES_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    known.add(line)
    except OSError:
        pass

    print(f"\n[3/3] Hits: {len(results)} repos with usable subscriptions")
    fresh = 0
    for r in results:
        subs = r["subscriptions"]
        new_subs = [s for s in subs if s["url"] not in known]
        r["is_new"] = bool(new_subs)
        if new_subs:
            fresh += 1
        flag = "NEW " if new_subs else "known"
        print(f"  [{flag}] {r['full_name']:<45} nodes={r['best_nodes']:<5} {subs[0]['url']}")

    dump_json(args.out, {"generated_at": int(time.time()),
                         "queries": QUERIES,
                         "repos_scanned": len(todo),
                         "hits": results})
    print(f"\nwrote {args.out}  ({len(results)} hits, {fresh} not in sources.txt)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
