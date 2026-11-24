"""Discover Telegram channels that publish free proxy nodes, then scrape them.

Two-stage:
  1) find channel handles  -- from a seed list + web search results (Bing/DDG)
  2) fetch https://t.me/s/<channel> public preview HTML (no login), extract
     proxy share links / subscription URLs from post text.

No Telegram account, no API key, no joining required.

Usage:
    python scripts/discover_telegram.py --out data/discovered_telegram.json
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from airport import paths  # noqa: E402

from scripts._probe_lib import (  # noqa: E402
    PROTO_SCHEMES,
    http_get,
    tcp_alive,
)

# Seed handles to try. Many will 404 or be private -- that is expected, the
# stage-1 filter throws them away.
SEED_HANDLES = [
    "freev2ray", "v2rayfree", "clashfree", "free_clash", "clashnode",
    "v2rayn", "v2raynodes", "freeairport", "airportfree", "jichang",
    "nodeshare", "share_node", "freefq", "fanqiang", "vpnfree",
    "clashnodefree", "freeproxy", "proxypool", "ssrshare", "vmess",
    "v2ray_configs", "clash_config", "freenode", "nodefree",
    "免费节点", "免费机场", "节点分享",
]

SEARCH_QUERIES = [
    "site:t.me clash 免费节点",
    "site:t.me vmess vless 订阅",
    "site:t.me 免费机场 节点",
    "site:t.me trojan hysteria2 节点",
    "site:t.me 白嫖节点",
]

HANDLE_RE = re.compile(r"t\.me/(?:s/)?([A-Za-z][A-Za-z0-9_]{4,31})")

PROXY_LINK_RE = re.compile(
    r"\b(?:" + "|".join(PROTO_SCHEMES) + r")://[^\s\"'<>\\]+", re.I
)
SUB_URL_RE = re.compile(
    r"https?://[^\s\"'<>()\]]+?(?:sub|clash|v2ray|vmess|nodes?|yaml|yml|txt)"
    r"[^\s\"'<>()\]]*",
    re.I,
)
POST_RE = re.compile(
    r'data-post="([^"]+)".*?tgme_widget_message_text[^>]*>(.*?)</div>',
    re.S,
)


def strip_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s)


def web_search(engine: str, query: str) -> list[str]:
    """Return candidate handles from search-engine result pages."""
    out: list[str] = []
    if engine == "bing":
        url = "https://www.bing.com/search?" + urllib.parse.urlencode(
            {"q": query, "count": 50}
        )
    else:
        url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode(
            {"q": query}
        )
    status, body, _ = http_get(url, timeout=25, retries=1)
    if status != 200:
        return out
    text = body.decode("utf-8", "replace")
    # Search engines wrap links; decode them before matching
    text = urllib.parse.unquote(text)
    for m in HANDLE_RE.finditer(text):
        out.append(m.group(1))
    return out


def check_channel(handle: str) -> dict | None:
    """Fetch t.me/s/<handle> and report what kind of channel it is."""
    url = f"https://t.me/s/{handle}"
    status, body, _ = http_get(url, timeout=20, retries=1)
    if status != 200 or len(body) < 500:
        return None
    text = body.decode("utf-8", "replace")
    if "tgme_widget_message_wrap" not in text:
        return None
    posts = POST_RE.findall(text)
    links: list[str] = []
    subs: list[str] = []
    for _post_id, raw in posts:
        plain = strip_html(raw)
        links.extend(PROXY_LINK_RE.findall(plain))
        subs.extend(SUB_URL_RE.findall(plain))
    title_m = re.search(r'<meta property="og:title" content="([^"]*)"', text)
    desc_m = re.search(r'<meta property="og:description" content="([^"]*)"', text)
    return {
        "handle": handle,
        "url": url,
        "title": html.unescape(title_m.group(1)) if title_m else "",
        "description": html.unescape(desc_m.group(1)) if desc_m else "",
        "posts_seen": len(posts),
        "proxy_links": len(links),
        "sub_links": len(set(subs)),
        "sample_links": links[:5],
        "sample_subs": list(dict.fromkeys(subs))[:8],
        "html_bytes": len(body),
    }


def dedupe(seq):
    seen = set()
    out = []
    for x in seq:
        k = x.lower()
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(paths.DATA_DIR / "discovered_telegram.json"))
    ap.add_argument("--skip-search", action="store_true")
    args = ap.parse_args()

    candidates: list[str] = list(SEED_HANDLES)

    if not args.skip_search:
        print(f"[1/3] web search: {len(SEARCH_QUERIES)} queries")
        for q in SEARCH_QUERIES:
            for engine in ("bing", "ddg"):
                try:
                    got = web_search(engine, q)
                except Exception as e:  # noqa: BLE001
                    print(f"  {engine} {q!r}: {e}")
                    continue
                print(f"  {engine} {q!r}: +{len(got)} handles")
                candidates.extend(got)
                time.sleep(1.5)

    candidates = dedupe([c for c in candidates if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", c)])
    print(f"\n[2/3] checking {len(candidates)} candidate channels via t.me/s/")

    alive = []
    for i, h in enumerate(candidates, 1):
        info = check_channel(h)
        if info:
            alive.append(info)
            if info["proxy_links"] or info["sub_links"]:
                print(
                    f"  [{i}/{len(candidates)}] HIT @{h} posts={info['posts_seen']} "
                    f"proxy_links={info['proxy_links']} subs={info['sub_links']}"
                )
        time.sleep(0.6)

    usable = [c for c in alive if c["proxy_links"] > 0 or c["sub_links"] > 0]
    usable.sort(key=lambda c: -(c["proxy_links"] + c["sub_links"]))

    print(f"\n[3/3] {len(alive)} public channels reachable, "
          f"{len(usable)} actually publish proxy data")

    # Split subscription hosts for TCP health check
    hosts = set()
    for c in usable:
        for s in c["sample_subs"]:
            m = re.match(r"https?://([^/:]+)", s)
            if m:
                hosts.add(m.group(1))

    print(f"\nTCP probing {len(hosts)} subscription hosts...")
    host_health = {}
    for h in sorted(hosts):
        ok = tcp_alive(h, 443) or tcp_alive(h, 80)
        host_health[h] = ok
        print(f"  {'OK  ' if ok else 'DEAD'} {h}")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "generated_at": int(time.time()),
                "candidates_tried": len(candidates),
                "channels_reachable": len(alive),
                "channels_with_proxy_data": len(usable),
                "usable": usable,
                "all_reachable": alive,
                "sub_host_health": host_health,
            },
            fh,
            ensure_ascii=False,
            indent=2,
        )
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
