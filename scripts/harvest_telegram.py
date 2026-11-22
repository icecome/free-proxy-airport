"""Harvest and health-check proxy data found in Telegram public channels.

Stage 2 of discover_telegram.py: take the channels that were found to publish
nodes, pull MORE pages of their preview timeline, and test what comes out.

Usage:
    python scripts/harvest_telegram.py --in data/discovered_telegram.json \
        --out data/harvested_telegram.json
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts._probe_lib import (  # noqa: E402
    dump_json,
    http_get,
    tcp_alive,
)
from scripts.discover_telegram import (  # noqa: E402
    POST_RE,
    PROXY_LINK_RE,
    SUB_URL_RE,
    strip_html,
)

VMESS_RE = re.compile(r"^vmess://([A-Za-z0-9+/=_-]+)$")


def vmess_endpoint(link: str) -> tuple[str, int] | None:
    m = VMESS_RE.match(link)
    if not m:
        return None
    raw = m.group(1)
    raw += "=" * (-len(raw) % 4)
    try:
        j = json.loads(base64.b64decode(raw))
    except Exception:  # noqa: BLE001
        return None
    host, port = j.get("add"), j.get("port")
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    if not host:
        return None
    return str(host), port


def uri_endpoint(link: str) -> tuple[str, int] | None:
    m = re.match(r"[a-z0-9]+://[^@]*@\[?([^\]/?#:]+)\]?:(\d+)", link, re.I)
    if not m:
        # trojan/trojan-go sometimes omit "@" creds
        m = re.match(r"[a-z0-9]+://(?:\S+@)?([^:/?#]+):(\d+)", link, re.I)
    if not m:
        return None
    try:
        return m.group(1), int(m.group(2))
    except ValueError:
        return None


def endpoint_of(link: str) -> tuple[str, int] | None:
    return vmess_endpoint(link) or uri_endpoint(link)


def fetch_channel(handle: str, pages: int = 3, per_page_sleep: float = 1.2) -> dict:
    """Walk the public preview backwards using ?before=<post_id>."""
    links: list[str] = []
    subs: list[str] = []
    seen_posts: set[str] = set()
    before = None
    title = ""
    for _page in range(pages):
        url = f"https://t.me/s/{handle}"
        if before:
            url += f"?before={before}"
        status, body, _ = http_get(url, timeout=20, retries=1)
        if status != 200:
            break
        text = body.decode("utf-8", "replace")
        if not title:
            m = re.search(r'<meta property="og:title" content="([^"]*)"', text)
            title = html.unescape(m.group(1)) if m else handle
        posts = POST_RE.findall(text)
        if not posts:
            break
        new = 0
        for post_id, raw in posts:
            if post_id in seen_posts:
                continue
            seen_posts.add(post_id)
            new += 1
            plain = strip_html(raw)
            links.extend(PROXY_LINK_RE.findall(plain))
            subs.extend(SUB_URL_RE.findall(plain))
        ids = [int(p.split("/")[-1]) for p, _ in posts if p.split("/")[-1].isdigit()]
        if not new or not ids:
            break
        before = min(ids)
        time.sleep(per_page_sleep)
    return {
        "handle": handle,
        "title": title,
        "posts": len(seen_posts),
        "links": list(dict.fromkeys(links)),
        "subs": list(dict.fromkeys(subs)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="data/discovered_telegram.json")
    ap.add_argument("--out", dest="out", default="data/harvested_telegram.json")
    ap.add_argument("--pages", type=int, default=3)
    args = ap.parse_args()

    disc = json.load(open(args.inp, encoding="utf-8"))
    handles = [c["handle"] for c in disc.get("usable", [])]
    if not handles:
        print("no channels with proxy data; nothing to harvest")
        return 0

    print(f"harvesting {len(handles)} channels x {args.pages} pages")
    harvested = []
    for h in handles:
        r = fetch_channel(h, pages=args.pages)
        print(f"  @{h:<20} posts={r['posts']:<4} links={len(r['links']):<4} subs={len(r['subs'])}")
        harvested.append(r)

    # ---- test subscription URLs ----
    print("\ntesting subscription URLs found in posts...")
    sub_results = []
    for r in harvested:
        for u in r["subs"]:
            status, body, _ = http_get(u, timeout=20, retries=1)
            sub_results.append(
                {
                    "channel": r["handle"],
                    "url": u,
                    "status": status,
                    "bytes": len(body),
                    "nodes": len(PROXY_LINK_RE.findall(
                        body.decode("utf-8", "replace")
                    )),
                }
            )
            print(f"  {status:>4} {len(body):>8}B nodes={sub_results[-1]['nodes']:<4} {u[:70]}")

    # ---- test individual proxy links ----
    print("\ntesting individual proxy links (TCP only)...")
    link_results = []
    for r in harvested:
        for link in r["links"]:
            ep = endpoint_of(link)
            if not ep:
                link_results.append({"channel": r["handle"], "link": link,
                                     "endpoint": None, "tcp": False})
                continue
            ok = tcp_alive(ep[0], ep[1], timeout=4)
            link_results.append(
                {
                    "channel": r["handle"],
                    "link": link[:200],
                    "endpoint": f"{ep[0]}:{ep[1]}",
                    "tcp": ok,
                }
            )

    total = len(link_results)
    alive = sum(1 for x in link_results if x["tcp"])
    print(f"  {alive}/{total} proxy links have a reachable TCP endpoint")
    by_ch = {}
    for x in link_results:
        c = by_ch.setdefault(x["channel"], [0, 0])
        c[0] += 1
        c[1] += 1 if x["tcp"] else 0
    for c, (n, a) in by_ch.items():
        print(f"    @{c:<20} {a}/{n} TCP alive")

    dump_json(
        args.out,
        {
            "harvested_at": int(time.time()),
            "channels": harvested,
            "subscriptions": sub_results,
            "links": link_results,
            "summary": {
                "channels": len(harvested),
                "proxy_links": total,
                "proxy_links_tcp_alive": alive,
                "subscriptions_serving_nodes": sum(
                    1 for s in sub_results if s["nodes"] >= 5
                ),
            },
        },
    )
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
