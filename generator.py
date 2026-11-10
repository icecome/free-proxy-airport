from __future__ import annotations

import base64
import gzip
import hashlib
import ipaddress
import json
import math
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
import urllib.parse

import requests
import yaml


VERSION = "v7.1"
OUTPUT_PATH = Path("output/clash.yaml")
# Health-check target. This URL is embedded in every proxy-group as `url`,
# so it is probed BY THE CLIENT, from inside mainland China, through each
# node. gstatic/google generate_204 are TCP-blackholed in CN (measured:
# connect timeout at t+6s on 2026-11-10) -> every group reports all-red and
# users read that as "the subscription is dead". cp.cloudflare.com answers
# 204 from CN in ~600ms and from the US runner alike.
TEST_URL = os.getenv("FREE_PROXY_AIRPORT_TEST_URL", "http://cp.cloudflare.com/generate_204")
# Egress proof executed on the GitHub runner (US vantage): a node that cannot
# reach a censored-target endpoint is a broken tunnel even if TCP connects.
URL_TEST_URL = os.getenv("FREE_PROXY_AIRPORT_URL_TEST_URL", "https://www.google.com/generate_204")
SOURCE_TIMEOUT = int(os.getenv("FREE_PROXY_AIRPORT_SOURCE_TIMEOUT", "12"))
LATENCY_TIMEOUT_MS = int(os.getenv("FREE_PROXY_AIRPORT_LATENCY_TIMEOUT_MS", "5000"))
MAX_RETRIES = 3
MAX_WORKERS = int(os.getenv("FREE_PROXY_AIRPORT_MAX_WORKERS", "48"))
MAX_CANDIDATES = int(os.getenv("FREE_PROXY_AIRPORT_MAX_CANDIDATES", "0"))
# Cloud-side bandwidth gate is opt-in now: the client-side url-test and the
# local cn_alive_check.py are the real quality filters; cloud only removes
# globally dead servers.
SPEED_GATE_ENABLED = os.getenv("FREE_PROXY_AIRPORT_SPEED_GATE", "1") == "1"
# Cap the published pool so client configs stay manageable.
MAX_OUTPUT_NODES = int(os.getenv("FREE_PROXY_AIRPORT_MAX_OUTPUT", "400"))
# CN-probe loop: queue size for the NAS worker, and the boost applied to
# nodes the NAS reported alive within the last hour.
PROBE_QUEUE_LIMIT = int(os.getenv("FREE_PROXY_AIRPORT_PROBE_QUEUE", "500"))
# Deprecated: CN verdicts now rank as tier-0 in publish_sort_key instead of
# adding a score bonus, which used to pin one frozen set above the 400-node
# cut line. Kept so old env files do not break; no longer read.
CN_ALIVE_BOOST = float(os.getenv("FREE_PROXY_AIRPORT_CN_BOOST", "1000.0"))
CANDIDATES_FILE = Path("data/candidates.json")
# How long a cloud-collected candidate set stays valid for phase B.
CANDIDATES_TTL_SECONDS = 26 * 3600
SPEED_TEST_URL = "https://speed.cloudflare.com/__down?bytes=4000000"
SPEED_TEST_MAX_BYTES = 3_000_000
SPEED_TEST_TIMEOUT = float(os.getenv("FREE_PROXY_AIRPORT_SPEED_TIMEOUT", "10"))
# Free nodes rarely sustain 10 Mbps; a 10 Mbps gate threw away working
# nodes and, worse, forced the "rescue slow nodes" path which re-admitted
# everything untested. 2 Mbps is the honest floor for usable browsing.
SPEED_TEST_MIN_MBPS = float(os.getenv("FREE_PROXY_AIRPORT_MIN_MBPS", "2.0"))
SPEED_TEST_TOP_N = int(os.getenv("FREE_PROXY_AIRPORT_SPEED_TOP_N", "60"))
SPEED_TEST_MIN_POOL = int(os.getenv("FREE_PROXY_AIRPORT_MIN_POOL", "24"))
# One download attempt per node keeps a full run inside the Actions budget
# (the old 2x25s x 150 nodes schedule needed ~105 min and could never finish
# before the next cron tick, which is what froze the published file).
SPEED_TEST_ATTEMPTS = int(os.getenv("FREE_PROXY_AIRPORT_SPEED_ATTEMPTS", "1"))
# Second egress gate. Off by default: the delay probe is already a real HTTP
# fetch through the node, this stage only re-proved it serially.
URL_TEST_STAGE = os.getenv("FREE_PROXY_AIRPORT_URL_TEST_STAGE", "0") == "1"
# Pure-Python fallback when the mihomo binary cannot be fetched (GitHub
# release assets return 403 from some networks). Without this the whole
# benchmark throws, main() sees empty metrics and republishes the last good
# file -- which is exactly how the subscription stayed frozen for 8 days.
TCP_FALLBACK_TIMEOUT_MS = int(os.getenv("FREE_PROXY_AIRPORT_TCP_TIMEOUT_MS", "2000"))
TCP_FALLBACK_WORKERS = int(os.getenv("FREE_PROXY_AIRPORT_TCP_WORKERS", "64"))
# Wall-clock cap for the mihomo archive; exceeded -> TCP fallback.
ENGINE_DOWNLOAD_TIMEOUT = int(os.getenv("FREE_PROXY_AIRPORT_ENGINE_TIMEOUT", "45"))
# Set to 1 to skip the mihomo binary entirely and always use the pure-Python
# TCP reachability gate. Useful on hosts where GitHub release assets are
# blocked (403) or stalled behind a TLS interceptor.
SKIP_ENGINE = os.getenv("FREE_PROXY_AIRPORT_SKIP_ENGINE", "0") == "1"
# Endpoint-level dedup budget. Free pools re-share one CDN host across a
# dozen different UUIDs; only one of those credentials is accepted
# server-side, so the rest are guaranteed-red entries that clients count as
# duplicates (measured on the live subscription: 74 nodes over 20 endpoints,
# one host carrying 12). Default 1 = one node per (family, server, port).
DEDUP_ENDPOINT_MAX = int(os.getenv("FREE_PROXY_AIRPORT_DEDUP_ENDPOINT", "1"))
# How many same-host copies may enter the benchmark per endpoint. 1 would
# discard the working credential when it is not the first one in source
# order; 3 covers the common "one host, several UUIDs" pattern cheaply.
DEDUP_ENDPOINT_CANDIDATES = int(os.getenv("FREE_PROXY_AIRPORT_DEDUP_CANDIDATES", "3"))
# A NAS verdict older than this must not keep pinning the same nodes to the
# top of the published file; the probe queue itself went 8 days stale and
# that is why the same handful of nodes shipped forever.
CN_ALIVE_FRESH_SECONDS = int(os.getenv("FREE_PROXY_AIRPORT_CN_FRESH", "7200"))

# One subscription URL per line. Lines starting with # are comments.
# Add a new free source -> append a line. A source that keeps failing -> delete its line.
SOURCES_FILE = Path(os.getenv("FREE_PROXY_AIRPORT_SOURCES", "sources.txt"))
# Legacy fallback list, only used when sources.txt is missing/empty.
# Kept in sync with sources.txt (2026-11-01 curation): dead sources removed.
DEFAULT_SOURCES = """\
# free-proxy-airport node sources: one URL per line, '#' starts a comment
# Verified 2026-11-10. openRunner/clash-freenode (sub.yaml removed, 404) and
# Flikify/Free-Node (both files 0 bytes) were dropped; snakem982 now serves
# source/clash-meta-2.yaml instead of the old main/clash.yaml.
https://raw.githubusercontent.com/snakem982/proxypool/main/source/clash-meta-2.yaml
https://raw.githubusercontent.com/cbusifabcap/daily_free_vpn/main/sub/V2Ray.yml
https://raw.githubusercontent.com/yy1588133/proxy-pool/main/clash.yaml
https://raw.githubusercontent.com/bily1258-design/beimi/main/output/clash.yaml
"""

SUPPORTED_PROXY_TYPES = {
    "ss",
    "ssr",
    "vmess",
    "vless",
    "trojan",
    "hysteria",
    "hysteria2",
    "hy2",
    "tuic",
    "socks5",
}

REQUIRED_GROUPS = (
    "AUTO-FAST",
    "HK-POOL",
    "JP-POOL",
    "US-POOL",
    "AI-POOL",
    "FALLBACK",
    "PROXY",
)


@dataclass
class ProxyMetric:
    proxy: dict[str, Any]
    latency: int
    region: str
    health_score: float
    speed_mbps: float = 0.0
    url_test_ok: bool = False
    # Set when a *fresh* NAS verdict says the node's endpoint is reachable
    # from mainland China. Ranks above unverified nodes without inflating
    # health_score (which used to pin one frozen set to the top forever).
    cn_verified: bool = False


class Sid(str):
    """REALITY short-id that must serialize as a quoted YAML string.

    PyYAML's resolver writes numeric-looking strings like '08' unquoted;
    mihomo's YAML 1.1 parser then reads them back as integers and rejects the
    whole config ("invalid REALITY short ID"). Wrapping in this subclass with
    an explicit representer forces quoting.
    """


yaml.SafeDumper.add_representer(
    Sid,
    lambda dumper, data: dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="'"),
)


def fetch_text(url: str, retries: int = MAX_RETRIES) -> str:
    headers = {
        "User-Agent": f"free-proxy-airport/{VERSION} (+https://github.com/)",
        "Accept": "text/plain, text/yaml, application/yaml, */*",
    }
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, headers=headers, timeout=SOURCE_TIMEOUT)
            response.raise_for_status()
            return response.content.decode("utf-8", errors="replace")
        except requests.exceptions.SSLError as exc:
            # Some CI/Windows hosts put a TLS interceptor with a private CA in
            # front of raw.githubusercontent.com; the default CA bundle then
            # rejects the cert while the plain content is fine. Sources are
            # public text files and node payloads are validated by
            # normalize_proxy() before use.
            try:
                response = requests.get(
                    url, headers=headers, timeout=SOURCE_TIMEOUT, verify=False
                )
                response.raise_for_status()
                return response.content.decode("utf-8", errors="replace")
            except Exception:
                pass
            last_error = exc
            if attempt < retries:
                time.sleep(2 * attempt)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and 400 <= status < 500:
                # Client error: retrying the same URL cannot succeed.
                raise RuntimeError(f"failed to fetch {url}: HTTP {status} (no retry)") from exc
            last_error = exc
            if attempt < retries:
                time.sleep(2 * attempt)
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def maybe_base64_decode(text: str) -> str:
    # Strip BOM and zero-width chars that break base64 sniffing on free sources.
    cleaned = text.lstrip("闂傚倸鍊烽悞锔锯偓绗涘洦鏅紓鍌欓檷閸旀垶绂嶉崼鏇炴槬闁逞屽墯閵囧嫰骞掗崱妞惧闂備線娼уΛ宀勫磻閹?00b\u200c\u200d").strip()
    compact = "".join(cleaned.split())
    if not compact or len(compact) % 4 != 0:
        return cleaned
    if not re.fullmatch(r"[A-Za-z0-9+/=\-_=]+", compact):
        return cleaned
    try:
        padded = compact + "=" * (-len(compact) % 4)
        decoded = base64.b64decode(padded, validate=True).decode("utf-8")
    except Exception:
        return cleaned
    return decoded if "proxies:" in decoded or "://" in decoded else cleaned


def load_yaml_document(text: str) -> Any:
    try:
        return yaml.safe_load(maybe_base64_decode(text))
    except yaml.YAMLError as exc:
        print(f"[WARN] YAML document parse failed: {exc}")
        return None


def extract_proxy_block(text: str) -> list[Any]:
    lines = maybe_base64_decode(text).splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if re.match(r"^proxies\s*:\s*$", line):
            start = index
            break
    if start is None:
        return []

    block: list[str] = []
    for line in lines[start + 1 :]:
        if line and not line.startswith((" ", "\t", "-")) and re.match(r"^[A-Za-z0-9_-]+\s*:", line):
            break
        block.append(line)

    try:
        parsed = yaml.safe_load("proxies:\n" + "\n".join(block))
    except yaml.YAMLError as exc:
        print(f"[WARN] proxy block parse failed: {exc}")
        return []
    if isinstance(parsed, dict) and isinstance(parsed.get("proxies"), list):
        return parsed["proxies"]
    return []


URI_SCHEME_TYPES = {
    "ss": "ss",
    "ssr": "ssr",
    "vmess": "vmess",
    "vless": "vless",
    "trojan": "trojan",
    "hysteria": "hysteria",
    "hysteria2": "hysteria2",
    "hy2": "hysteria2",
    "tuic": "tuic",
    "socks": "socks5",
    "socks5": "socks5",
}

# Fields where URL percent-encoding must be decoded back to raw text.
URI_DECODE_FIELDS = {"name", "password", "uuid", "servername", "sni", "host"}


def parse_proxy_uri(uri: str) -> dict[str, Any] | None:
    """Parse one vless:// vmess:// trojan:// ss:// hysteria2:// tuic:// share URI."""
    scheme, _, body = uri.partition("://")
    scheme = scheme.lower().strip()
    if scheme not in URI_SCHEME_TYPES or not body:
        return None

    # name is the URL fragment after '#'
    name = ""
    if "#" in body:
        body, name = body.split("#", 1)
        name = urllib.parse.unquote(name).strip()

    proxy_type = URI_SCHEME_TYPES[scheme]

    if scheme == "vmess":
        # vmess://base64(JSON) with fields {"add","port","id","ps",...}
        raw = body.split("#", 1)[0]
        try:
            padded = raw + "=" * (-len(raw) % 4)
            data = json.loads(base64.b64decode(padded, validate=True).decode("utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        proxy = {
            "name": str(data.get("ps") or name or "vmess-node"),
            "type": "vmess",
            "server": str(data.get("add", "")),
            "port": data.get("port"),
            "uuid": str(data.get("id", "")),
            "alterId": int(data.get("aid", 0) or 0),
            "cipher": str(data.get("scy") or "auto"),
            "tls": bool(data.get("tls") in ("tls", True)),
        }
        net = str(data.get("net", "tcp"))
        if net in ("ws", "grpc", "h2"):
            proxy["network"] = net
            if net == "ws":
                proxy["ws-opts"] = {"path": str(data.get("path", "/"))}
        return proxy if proxy["server"] and proxy["port"] else None

    # Legacy whole-line-base64 ss variant: ss://base64(method:password@host:port)#name
    if scheme == "ss" and "@" not in body:
        try:
            padded = body + "=" * (-len(body) % 4)
            decoded_line = base64.b64decode(padded, validate=True).decode("utf-8")
        except Exception:
            return None
        secret, _, hostport = decoded_line.rpartition("@")
        host, _, port_str = hostport.rpartition(":")
        host = host.strip("[]")
        try:
            port = int(port_str)
        except ValueError:
            return None
        if not host or port <= 0 or port > 65535:
            return None
        method, _, pwd = secret.partition(":")
        if not method or not pwd:
            return None
        return {
            "name": name or f"ss-{host}",
            "type": "ss",
            "server": host,
            "port": port,
            "cipher": method,
            "password": pwd,
        }

    # generic user@host:port?query style: vless/trojan/ss/hysteria2/tuic/socks
    query: dict[str, str] = {}
    if "?" in body:
        body, qs = body.split("?", 1)
        for pair in qs.split("&"):
            if "=" in pair:
                k, _, v = pair.partition("=")
                query[k] = urllib.parse.unquote(v)

    userinfo = ""
    if "@" in body:
        userinfo, _, hostport = body.rpartition("@")
    else:
        # ss style without userinfo: base64(method:password)@host:port already
        # handled above; some sources emit host:port only -> skip
        hostport = body
        if "@" not in body and scheme == "ss" and ":" not in body:
            return None

    host, _, port_str = hostport.rpartition(":")
    host = host.strip("[]")
    try:
        port = int(port_str)
    except ValueError:
        return None
    if not host or port <= 0 or port > 65535:
        return None

    proxy: dict[str, Any] = {
        "name": name or f"{proxy_type}-{host}",
        "type": proxy_type,
        "server": host,
        "port": port,
    }

    if proxy_type == "ss":
        # SIP002: userinfo may be base64(method:password) or plain method:password
        secret = userinfo
        try:
            padded = secret + "=" * (-len(secret) % 4)
            secret = base64.b64decode(padded, validate=True).decode("utf-8")
        except Exception:
            pass
        method, _, pwd = secret.partition(":")
        if not method or not pwd:
            return None
        proxy["cipher"] = method
        proxy["password"] = pwd
    elif proxy_type in ("vless", "trojan", "tuic"):
        proxy["password" if proxy_type == "trojan" else "uuid"] = userinfo
        if proxy_type == "tuic":
            proxy["uuid"] = userinfo.partition(":")[0]
            pwd = userinfo.partition(":")[2]
            if pwd:
                proxy["password"] = pwd
    elif proxy_type in ("hysteria", "hysteria2", "socks5"):
        proxy["password"] = userinfo

    if query:
        if query.get("security") in ("tls", "reality") or proxy_type in ("trojan", "hysteria", "hysteria2", "tuic"):
            # reality implies TLS: mihomo rejects "REALITY requires TLS" otherwise
            proxy["tls"] = True
        if query.get("security") == "reality":
            proxy["tls"] = True
        if query.get("sni"):
            proxy["sni"] = query["sni"]
        if query.get("servername") and "sni" not in proxy:
            proxy["sni"] = query["servername"]
        if query.get("flow"):
            proxy["flow"] = query["flow"]
        if query.get("type") in ("ws", "grpc", "tcp", "http"):
            if query["type"] in ("ws", "grpc", "http"):
                proxy["network"] = query["type"]
            if query["type"] == "ws" and query.get("path"):
                proxy["ws-opts"] = {"path": query["path"]}
                if query.get("host"):
                    proxy["ws-opts"]["headers"] = {"Host": query["host"]}
        if query.get("fp"):
            proxy["client-fingerprint"] = query["fp"]
        if query.get("pbk"):
            proxy["reality-opts"] = {"public-key": query["pbk"], **({"short-id": query["sid"]} if query.get("sid") else {})}

    for field in URI_DECODE_FIELDS:
        if field in proxy and isinstance(proxy[field], str):
            proxy[field] = urllib.parse.unquote(proxy[field])
    return proxy


def parse_proxy_uri_lines(text: str) -> list[dict[str, Any]]:
    """Extract share-URI nodes (one per line) from decoded subscription text."""
    proxies: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "://" not in line:
            continue
        parsed = parse_proxy_uri(line)
        if parsed:
            proxies.append(parsed)
    return proxies


def extract_proxies(text: str) -> list[dict[str, Any]]:
    """Auto-detect the payload format and extract proxy dicts.

    Supported formats, tried in order:
      1. Clash YAML (full document with proxies: or a bare list)
      2. Clash YAML proxy block embedded in larger text
      3. Base64-encoded (1)/(3): decoded then parsed as URI list
      4. Plain share-URI list (vless:// vmess:// trojan:// ss:// ssr://
         hysteria:// hysteria2:// hy2:// tuic:// socks5://, one per line)
    """
    decoded = maybe_base64_decode(text)

    document = load_yaml_document(text)
    proxies: list[Any] = []
    if isinstance(document, dict):
        proxies = document.get("proxies", [])
    elif isinstance(document, list):
        proxies = document

    if not proxies:
        proxies = extract_proxy_block(text)

    if not proxies:
        proxies = parse_proxy_uri_lines(decoded)

    clean: list[dict[str, Any]] = []
    for proxy in proxies:
        if isinstance(proxy, dict):
            clean.append(dict(proxy))
    return clean


def unique_ordered(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def load_source_urls() -> list[str]:
    """Read subscription URLs from sources.txt (one per line, '#' comments).

    Falls back to the built-in defaults when the file is missing or empty,
    and writes the defaults out so the user can edit them in place.
    """
    text: str | None = None
    if SOURCES_FILE.exists():
        text = SOURCES_FILE.read_text(encoding="utf-8")

    if text is None:
        SOURCES_FILE.write_text(DEFAULT_SOURCES, encoding="utf-8")
        print(f"[INFO] created {SOURCES_FILE} with default sources; edit it to add/remove sources")
        text = DEFAULT_SOURCES

    urls = unique_ordered(
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    if not urls:
        print(f"[WARN] {SOURCES_FILE} has no usable URLs; falling back to defaults")
        urls = unique_ordered(
            line.strip()
            for line in DEFAULT_SOURCES.splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
    return urls


def collect_proxies() -> tuple[int, list[dict[str, Any]], list[tuple[str, bool]]]:
    source_report: list[tuple[str, bool]] = []
    collected: list[dict[str, Any]] = []
    for url in load_source_urls():
        try:
            text = fetch_text(url)
            found = extract_proxies(text)
            print(f"[OK] proxies={len(found)} url={url}")
            if found:
                collected.extend(found)
                source_report.append((url, True))
            else:
                source_report.append((url, False))
        except Exception as exc:
            print(f"[WARN] source skipped url={url} error={exc}")
            source_report.append((url, False))

    sanitized = sanitize_and_deduplicate(collected)
    if MAX_CANDIDATES > 0 and len(sanitized) > MAX_CANDIDATES:
        print(f"[WARN] limiting candidates from {len(sanitized)} to {MAX_CANDIDATES}")
        sanitized = sanitized[:MAX_CANDIDATES]
    return len(collected), sanitized, source_report


def sanitize_and_deduplicate(proxies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_fingerprints: set[str] = set()
    seen_names: set[str] = set()
    endpoint_counts: dict[tuple[str, str, int], int] = {}
    dropped_same_host = 0
    result: list[dict[str, Any]] = []

    for index, raw in enumerate(proxies, start=1):
        proxy = normalize_proxy(raw, index)
        if not proxy:
            continue

        fingerprint = proxy_fingerprint(proxy)
        if fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(fingerprint)

        # Keep a few copies per host so the benchmark can still discover which
        # credential the server actually accepts, but never let one CDN edge
        # flood the candidate set: 20 hosts x 12 UUIDs used to eat the whole
        # output budget while only one credential per host ever worked.
        key = client_endpoint_key(proxy)
        if endpoint_counts.get(key, 0) >= DEDUP_ENDPOINT_CANDIDATES:
            dropped_same_host += 1
            continue
        endpoint_counts[key] = endpoint_counts.get(key, 0) + 1

        base_name = str(proxy["name"]).strip() or f"node-{index}"
        name = base_name
        suffix = 2
        while name in seen_names:
            name = f"{base_name}-{suffix}"
            suffix += 1
        proxy["name"] = name
        seen_names.add(name)
        result.append(proxy)

    if dropped_same_host:
        print(
            f"[INFO] candidate filter: dropped {dropped_same_host} extra copies of "
            f"already-covered hosts (cap {DEDUP_ENDPOINT_CANDIDATES}/endpoint); "
            f"{len(result)} unique candidates remain"
        )
    return result


def dedupe_published_by_endpoint(metrics: list[ProxyMetric]) -> list[ProxyMetric]:
    """Collapse published nodes to one per (family, server, port).

    Metrics arrive sorted by score, so the surviving node is the one that
    actually passed the throughput/egress gates. Without this, the same CDN
    host ships a dozen entries, of which all but one are guaranteed-red in the
    client — that is the "去重了还是重复" symptom users see.
    """
    if DEDUP_ENDPOINT_MAX <= 0:
        return metrics
    kept: list[ProxyMetric] = []
    used: dict[tuple[str, int], int] = {}
    merged = 0
    for metric in metrics:
        key = client_endpoint_key(metric.proxy)
        if used.get(key, 0) >= DEDUP_ENDPOINT_MAX:
            merged += 1
            continue
        used[key] = used.get(key, 0) + 1
        kept.append(metric)
    if merged:
        print(
            f"[INFO] endpoint dedup at publish: {len(metrics)} -> {len(kept)} "
            f"(dropped {merged} shadow copies of the same host)"
        )
    return kept


def is_non_routable_server(value: Any) -> bool:
    """Reject addresses that can never be a public proxy endpoint.

    Free subscription sources leak nodes whose `server` is a loopback /
    unspecified / link-local / multicast / reserved / private literal, or the
    literal hostnames `localhost` / `::1`. Published as-is they are guaranteed
    dead entries: the client dials its own machine (or nothing at all), and in
    the worst case points traffic at a service on the user's LAN.

    IPv4 127.0.0.0/8 is covered by ipaddress.is_loopback (127.0.0.53 included),
    so the check is not a hard-coded single address.

    Hostnames are NOT rejected here: most nodes use a DNS name, and resolving
    every one of them at collection time is both slow and unreliable. A name
    that resolves to a private address is caught by the TCP/egress gates
    downstream.
    """
    text = str(value or "").strip().strip("[]")
    if not text:
        return True
    lowered = text.lower()
    if lowered in {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}:
        return True
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        # Not an IP literal: treat as a hostname and let the benchmark decide.
        return False
    return bool(
        ip.is_loopback
        or ip.is_unspecified
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_private
    )


def normalize_proxy(raw: dict[str, Any], index: int) -> dict[str, Any] | None:
    proxy = {key: value for key, value in raw.items() if value is not None}
    proxy_type = str(proxy.get("type", "")).lower().strip()
    if proxy_type not in SUPPORTED_PROXY_TYPES:
        return None

    if proxy_type == "hy2":
        proxy_type = "hysteria2"
    proxy["type"] = proxy_type

    # mihomo refuses to load the whole config if any node has REALITY options
    # without TLS (observed cloud failure "proxy 535: REALITY requires TLS").
    # Sources emit this broken combination regularly, so repair it here for
    # every node regardless of origin (URI-parsed or YAML).
    if "reality-opts" in proxy and proxy.get("tls") is not True:
        proxy["tls"] = True

    # mihomo hard-requires up/down bandwidth fields for hysteria(2)
    # ("unset fields: down, up"); free sources omit them constantly.
    if proxy_type in ("hysteria", "hysteria2"):
        proxy.setdefault("up", "100 Mbps")
        proxy.setdefault("down", "200 Mbps")

    # ss 2022-blake3 ciphers demand a base64-encoded key; sources sometimes
    # ship raw keys, which mihomo rejects at load time ("decode key").
    if proxy_type == "ss" and str(proxy.get("cipher", "")).startswith("2022-blake3"):
        password = str(proxy.get("password", ""))
        try:
            base64.b64decode(password + "=" * (-len(password) % 4), validate=True)
        except Exception:
            return None  # invalid key: mihomo refuses the whole config otherwise

    # REALITY short-id: mihomo wants hex, max 8 bytes. Two hazards here:
    # 1. garbage values ("invalid REALITY short ID") -> drop the node
    # 2. PyYAML writes numeric-looking strings like '08'/'09' UNQUOTED,
    #    mihomo's YAML 1.1 parser then reads them as integers -> same error.
    #    Fix by canonicalizing: strip leading zeros and re-encode as a hex
    #    string prefixed with '0x'? No - sid must be plain hex. Instead wrap
    #    the value so safe_dump ALWAYS emits it quoted (see write_config for
    #    the representer).
    reality = proxy.get("reality-opts")
    if isinstance(reality, dict) and "short-id" in reality:
        sid = str(reality.get("short-id") or "").strip().lower()
        if sid and re.fullmatch(r"[0-9a-f]{1,16}", sid) and int(sid, 16) > 0:
            reality["short-id"] = Sid(reality["short-id"])
        elif sid:
            return None  # garbage short-id poisons the whole mihomo config
        else:
            del reality["short-id"]

    name = str(proxy.get("name", "")).strip() or f"node-{index}"
    server = str(proxy.get("server", "")).strip()
    if not server:
        return None
    if is_non_routable_server(server):
        # Upstream sources leak loopback/literal-private nodes (observed:
        # vmess 127.0.0.53:80 uuid 88888888-...). Drop at the single ingress
        # so they never reach fingerprints, probe queue or published config.
        return None

    try:
        port = int(proxy.get("port"))
    except Exception:
        return None
    if port <= 0 or port > 65535:
        return None

    proxy["name"] = name
    proxy["server"] = server
    proxy["port"] = port
    return proxy


def proxy_fingerprint(proxy: dict[str, Any]) -> str:
    important = {
        "type": proxy.get("type"),
        "server": proxy.get("server"),
        "port": proxy.get("port"),
        "uuid": proxy.get("uuid"),
        "password": proxy.get("password"),
        "cipher": proxy.get("cipher"),
        "network": proxy.get("network"),
    }
    payload = json.dumps(important, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# Protocol families that dial the same transport on the same socket. Used to
# recognise "same host, different credential/alias" copies that survive the
# credential-aware fingerprint.
PROTOCOL_FAMILY = {
    "ss": "ss",
    "ssr": "ss",
    "vmess": "vmess",
    "vless": "vless",
    "trojan": "trojan",
    "hysteria": "hysteria2",
    "hysteria2": "hysteria2",
    "hy2": "hysteria2",
    "tuic": "tuic",
    "socks": "socks5",
    "socks5": "socks5",
}


def endpoint_key(proxy: dict[str, Any]) -> tuple[str, str, int]:
    """Client-visible identity of a node: (protocol family, host, port).

    Two nodes with the same key differ only in credential/name, so at most one
    of them can be the accepted one — publishing both is what clients show as
    duplicate entries where all but one are red.

    NOTE: the key deliberately keeps the family out of the dedup decision at
    publish time (see dedupe_published_by_endpoint): from the client's point
    of view two entries with the same host:port are the same server, whatever
    protocol each claims. Returning the family here would let e.g. an ss and
    a trojan node on the same 443 slip through as "different" nodes.
    """
    raw_type = str(proxy.get("type", "")).strip().lower()
    family = PROTOCOL_FAMILY.get(raw_type, raw_type)
    host = str(proxy.get("server", "")).strip().lower()
    try:
        port = int(proxy.get("port", 0))
    except (TypeError, ValueError):
        port = 0
    return (family, host, port)


def client_endpoint_key(proxy: dict[str, Any]) -> tuple[str, int]:
    """What the client actually lists: (host, port), protocol-independent."""
    _, host, port = endpoint_key(proxy)
    return (host, port)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def find_or_install_mihomo() -> Path:
    for name in ("mihomo", "clash-meta", "clash"):
        found = shutil.which(name)
        if found:
            print(f"[OK] using proxy engine: {found}")
            return Path(found)

    install_dir = Path(tempfile.gettempdir()) / "free-proxy-airport-mihomo"
    install_dir.mkdir(parents=True, exist_ok=True)
    binary = install_dir / ("mihomo.exe" if os.name == "nt" else "mihomo")
    if binary.exists():
        print(f"[OK] using cached proxy engine: {binary}")
        return binary

    url, expected_sha256 = select_mihomo_asset()
    print(f"[INFO] downloading proxy engine: {url}")
    archive = download_file(url, install_dir)
    if expected_sha256:
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual != expected_sha256:
            archive.unlink(missing_ok=True)
            raise RuntimeError(
                f"mihomo archive integrity check FAILED: expected {expected_sha256}, got {actual}"
            )
        print(f"[OK] mihomo archive sha256 verified: {actual[:16]}...")
    else:
        print("[WARN] mihomo release has no digest for this asset; proceeding without verification")
    extracted = extract_mihomo_binary(archive, install_dir)
    extracted.chmod(extracted.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if extracted != binary:
        shutil.copy2(extracted, binary)
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return binary


def select_mihomo_asset() -> tuple[str, str | None]:
    """Pick the best mihomo release asset URL.

    Returns (url, expected_sha256). The digest comes from the GitHub API's
    per-asset `digest` field (sha256:<hex>) when available; None otherwise.
    """
    api_url = "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest"
    headers = {"User-Agent": "free-proxy-airport"}
    try:
        data = requests.get(api_url, headers=headers, timeout=SOURCE_TIMEOUT).json()
    except requests.exceptions.SSLError:
        # Same private-CA interceptor case as fetch_text(); the release
        # metadata is public and the downloaded archive is sha256-checked.
        data = requests.get(api_url, headers=headers, timeout=SOURCE_TIMEOUT, verify=False).json()
    assets = data.get("assets", [])
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "darwin":
        os_token = "darwin"
    elif system == "linux":
        os_token = "linux"
    elif system == "windows":
        os_token = "windows"
    else:
        raise RuntimeError(f"unsupported OS for Mihomo download: {system}")

    if machine in {"x86_64", "amd64"}:
        arch_tokens = ["amd64-compatible", "amd64"]
    elif machine in {"arm64", "aarch64"}:
        arch_tokens = ["arm64"]
    else:
        raise RuntimeError(f"unsupported architecture for Mihomo download: {machine}")

    candidates: list[tuple[int, str, str | None]] = []
    for asset in assets:
        name = str(asset.get("name", "")).lower()
        download_url = str(asset.get("browser_download_url", ""))
        if not download_url:
            continue
        if os_token not in name:
            continue
        if not any(token in name for token in arch_tokens):
            continue
        if not (name.endswith(".gz") or name.endswith(".zip")):
            continue
        score = 0
        if "compatible" in name:
            score += 10
        if "go120" not in name:
            score += 2
        digest = str(asset.get("digest", "") or "")
        expected_sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
        candidates.append((score, download_url, expected_sha))

    if not candidates:
        raise RuntimeError("no matching Mihomo release asset found")
    candidates.sort(reverse=True)
    return candidates[0][1], candidates[0][2]


def download_file(url: str, directory: Path) -> Path:
    """Download with a wall-clock cap.

    A TLS interceptor can leave `requests.get(stream=True)` hanging forever
    even with per-socket timeouts; cap the whole transfer so a blocked binary
    degrades to the TCP fallback instead of freezing the entire run.
    """
    target = directory / Path(url.split("?")[0]).name
    keywords = [dict(verify=True), dict(verify=False)]
    last_error: Exception | None = None
    for kw in keywords:
        try:
            with requests.get(
                url, stream=True, timeout=(6, SOURCE_TIMEOUT), **kw
            ) as resp:
                resp.raise_for_status()
                started = time.monotonic()
                with target.open("wb") as file:
                    for chunk in resp.iter_content(chunk_size=1024 * 512):
                        if chunk:
                            file.write(chunk)
                        if time.monotonic() - started > ENGINE_DOWNLOAD_TIMEOUT:
                            raise TimeoutError(
                                f"engine download exceeded {ENGINE_DOWNLOAD_TIMEOUT}s"
                            )
            if target.stat().st_size > 0:
                return target
        except requests.exceptions.SSLError as exc:
            last_error = exc
            continue
        except Exception as exc:
            last_error = exc
            break
    target.unlink(missing_ok=True)
    raise RuntimeError(f"failed to download {url}: {last_error}")


def extract_mihomo_binary(archive: Path, directory: Path) -> Path:
    if archive.suffix == ".gz" and not archive.name.endswith(".tar.gz"):
        target = directory / archive.name[:-3]
        with gzip.open(archive, "rb") as source, target.open("wb") as dest:
            shutil.copyfileobj(source, dest)
        return target

    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zipped:
            zipped.extractall(directory)
        for path in directory.rglob("*"):
            if path.is_file() and "mihomo" in path.name.lower():
                return path

    raise RuntimeError(f"unsupported Mihomo archive: {archive}")


def write_benchmark_config(path: Path, proxies: list[dict[str, Any]], controller_port: int) -> int:
    names = [str(proxy["name"]) for proxy in proxies]
    mixed_port = find_free_port()
    config = {
        "mixed-port": mixed_port,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "external-controller": f"127.0.0.1:{controller_port}",
        "proxies": proxies,
        "proxy-groups": [
            {
                "name": "BENCHMARK",
                "type": "select",
                "proxies": names or ["DIRECT"],
            }
        ],
        "rules": ["MATCH,BENCHMARK"],
    }
    path.write_bytes(yaml.safe_dump(config, allow_unicode=True, sort_keys=False).encode("utf-8"))
    return mixed_port


def wait_for_controller(controller_url: str, process: subprocess.Popen[str]) -> None:
    last_error: Exception | None = None
    for _ in range(60):
        if process.poll() is not None:
            raise RuntimeError(f"Mihomo exited before controller became ready (code={process.returncode})")
        try:
            response = requests.get(f"{controller_url}/version", timeout=1)
            if response.status_code == 200:
                return
        except Exception as exc:
            last_error = exc
        time.sleep(0.5)
    raise RuntimeError(f"Mihomo controller did not become ready; last probe error: {last_error}")


def benchmark_single_batch(
    engine: Path,
    proxies: list[dict[str, Any]],
    temp_root: Path,
) -> tuple[list[ProxyMetric], str | None]:
    """Run one mihomo instance over one batch of proxies.

    Returns (metrics, error_message). error_message is None on success;
    on engine startup failure it carries the stderr tail and metrics is [].
    """
    batch_dir = temp_root / f"batch-{abs(hash(tuple(sorted(p['name'] for p in proxies[:1])))) % 100000}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    config_path = batch_dir / "benchmark.yaml"
    controller_port = find_free_port()
    controller_url = f"http://127.0.0.1:{controller_port}"
    mixed_port = write_benchmark_config(config_path, proxies, controller_port)

    engine_log = batch_dir / "mihomo-stderr.log"
    with engine_log.open("wb") as err_file:
        process = subprocess.Popen(
            [str(engine), "-d", str(batch_dir), "-f", str(config_path)],
            stdout=subprocess.DEVNULL,
            stderr=err_file,
        )
        try:
            wait_for_controller(controller_url, process)
            metrics = run_delay_tests(controller_url, proxies)
            if metrics and SPEED_GATE_ENABLED:
                metrics = run_speed_tests(controller_url, mixed_port, metrics)
                for metric in metrics:
                    metric.health_score = health_score(
                        str(metric.proxy.get("name", "")),
                        metric.latency,
                        metric.region,
                        metric.speed_mbps,
                    )
                metrics.sort(key=publish_sort_key)
            return metrics, None
        except RuntimeError as exc:
            # Engine failed to start / died: capture stderr for diagnosis.
            stderr_text = engine_log.read_text(encoding="utf-8", errors="replace")[-4000:]
            tail = " | ".join(stderr_text.splitlines()[-8:])
            return [], f"{exc}; stderr tail: {tail}"
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            if process.returncode not in (0, None):
                stderr_text = engine_log.read_text(encoding="utf-8", errors="replace")[-4000:]
                print(f"[ERROR] mihomo exited with code {process.returncode}; stderr tail:")
                for line in stderr_text.splitlines()[-20:]:
                    print(f"  | {line}")


def tcp_reachable(proxy: dict[str, Any]) -> tuple[bool, int]:
    """Measure a TCP connect to the node's entry, no external engine needed.

    Does not prove the tunnel works end-to-end (that needs a real proxy
    client), but it removes the ~62% of published nodes whose port is simply
    dead from the client's network. Combined with the client-side url-test
    group this is a usable fallback when mihomo cannot be installed.
    """
    host = str(proxy.get("server", "")).strip()
    try:
        port = int(proxy.get("port", 0))
    except (TypeError, ValueError):
        return False, 0
    if not host or port <= 0 or port > 65535:
        return False, 0
    deadline = TCP_FALLBACK_TIMEOUT_MS / 1000
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=deadline):
            return True, max(1, int((time.monotonic() - start) * 1000))
    except OSError:
        return False, 0


def tcp_fallback_benchmark(proxies: list[dict[str, Any]]) -> list[ProxyMetric]:
    """Rank candidates by direct TCP reachability + region.

    Used only when find_or_install_mihomo() fails; it never leaves the
    pipeline with an empty metric list, so the "republish the frozen file"
    branch is not reached.
    """
    metrics: list[ProxyMetric] = []
    workers = max(1, min(TCP_FALLBACK_WORKERS, len(proxies)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(tcp_reachable, p): i for i, p in enumerate(proxies)}
        verdicts: dict[int, tuple[bool, int]] = {
            futures[f]: f.result() for f in as_completed(futures)
        }
    for index, (alive, latency) in verdicts.items():
        if not alive:
            continue
        proxy = proxies[index]
        region = detect_region(str(proxy.get("name", "")))
        metrics.append(
            ProxyMetric(
                proxy=proxy,
                latency=latency,
                region=region,
                health_score=health_score(str(proxy.get("name", "")), latency, region),
                url_test_ok=True,
            )
        )
    print(f"[INFO] TCP fallback: {len(metrics)}/{len(proxies)} reachable (no mihomo needed)")
    return metrics


def benchmark_proxies(proxies: list[dict[str, Any]]) -> list[ProxyMetric]:
    """Benchmark candidates in batches so one poisoned batch cannot take
    down the whole run (a single invalid proxy makes mihomo refuse to start).

    If the engine binary is unavailable (403 / blocked release assets), fall
    back to pure-Python TCP reachability instead of returning nothing.
    """
    if not proxies:
        return []

    global _CN_ALIVE_NAMES
    _CN_ALIVE_NAMES = {r.get("server", "") + ":" + str(r.get("port", "")) for r in get_cn_alive_cached().values()}
    if SKIP_ENGINE:
        print("[INFO] FREE_PROXY_AIRPORT_SKIP_ENGINE=1: using TCP fallback directly")
        return tcp_fallback_benchmark(proxies)
    try:
        engine = find_or_install_mihomo()
    except Exception as exc:
        print(f"[WARN] mihomo unavailable ({type(exc).__name__}: {exc}); using TCP fallback")
        return tcp_fallback_benchmark(proxies)
    batch_size = 300
    all_metrics: list[ProxyMetric] = []
    batches = [proxies[i : i + batch_size] for i in range(0, len(proxies), batch_size)]
    print(f"[INFO] benchmarking {len(proxies)} candidates in {len(batches)} batch(es) of <= {batch_size}")

    with tempfile.TemporaryDirectory(prefix="free-proxy-airport-") as temp_name:
        temp_root = Path(temp_name)
        for index, batch in enumerate(batches, start=1):
            metrics, error = benchmark_single_batch(engine, batch, temp_root)
            print(f"[INFO] batch {index}/{len(batches)}: {len(metrics)} passed"
                  + (f" (batch FAILED: {error[:300]})" if error else ""))
            all_metrics.extend(metrics)

    if not all_metrics:
        # Every batch failed (e.g. binary starts but rejects the config, or
        # the controller never comes up). Do NOT return an empty list: main()
        # would treat that as "benchmark failed" and republish the frozen
        # subscription again.
        print("[WARN] all mihomo batches failed; using TCP fallback instead of stale republish")
        return tcp_fallback_benchmark(proxies)

    all_metrics.sort(key=publish_sort_key)
    return all_metrics


def run_delay_tests(controller_url: str, proxies: list[dict[str, Any]]) -> list[ProxyMetric]:
    workers = max(1, min(MAX_WORKERS, len(proxies)))
    metrics: list[ProxyMetric] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(test_single_proxy, controller_url, proxy): proxy
            for proxy in proxies
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            proxy = futures[future]
            try:
                metric = future.result()
            except Exception as exc:
                print(f"[DROP] {proxy.get('name')} failed: {exc}")
                continue
            if metric:
                metrics.append(metric)
            if completed % 25 == 0 or completed == len(futures):
                print(f"[INFO] tested {completed}/{len(futures)} kept={len(metrics)}")
    metrics.sort(key=publish_sort_key)
    return metrics


def test_single_proxy(controller_url: str, proxy: dict[str, Any]) -> ProxyMetric | None:
    name = str(proxy["name"])
    url = (
        f"{controller_url}/proxies/{quote(name, safe='')}/delay"
        f"?timeout={LATENCY_TIMEOUT_MS}&url={quote(TEST_URL, safe='')}"
    )
    response = requests.get(url, timeout=(LATENCY_TIMEOUT_MS / 1000) + 3)
    if response.status_code != 200:
        return None
    data = response.json()
    latency = int(data.get("delay", 0))
    if latency <= 0 or latency > LATENCY_TIMEOUT_MS:
        return None
    region = detect_region(name)
    score = health_score(name, latency, region)
    return ProxyMetric(proxy=proxy, latency=latency, region=region, health_score=score)


def switch_benchmark_node(controller_url: str, name: str) -> bool:
    """Point the BENCHMARK selector group at a specific proxy."""
    try:
        response = requests.put(
            f"{controller_url}/proxies/{quote('BENCHMARK', safe='')}",
            json={"name": name},
            timeout=5,
        )
        return response.status_code == 204 or response.status_code == 200
    except Exception:
        return False


def download_speed_probe(proxy_port: int) -> float | None:
    """Download a fixed-size file through the local mixed-port once.

    Returns throughput in Mbps, or None if the attempt is unusable.
    """
    proxies = {
        "http": f"http://127.0.0.1:{proxy_port}",
        "https": f"http://127.0.0.1:{proxy_port}",
    }
    received = 0
    start = time.monotonic()
    try:
        with requests.get(
            SPEED_TEST_URL,
            stream=True,
            proxies=proxies,
            timeout=SPEED_TEST_TIMEOUT,
            headers={"User-Agent": f"free-proxy-airport/{VERSION}"},
        ) as response:
            if response.status_code != 200:
                return None
            for chunk in response.iter_content(chunk_size=65536):
                received += len(chunk)
                if received >= SPEED_TEST_MAX_BYTES:
                    break
    except Exception:
        return None
    elapsed = time.monotonic() - start
    if elapsed < 0.5 or received < 500_000:
        return None
    return received * 8 / elapsed / 1_000_000


def measure_speed_mbps(controller_url: str, proxy_port: int, proxy: dict[str, Any]) -> float:
    """Single-connection download through one proxy node.

    The CN-alive short circuit used to compare the node NAME against a set of
    "server:port" strings, which can never match — every NAS-verified node
    still burned a full download probe, and the 105-minute runs that froze the
    schedule were the result. Compare on the endpoint instead.
    """
    name = str(proxy["name"])
    if not switch_benchmark_node(controller_url, name):
        return 0.0
    endpoint = str(proxy.get("server", "")) + ":" + str(proxy.get("port", ""))
    if endpoint in _CN_ALIVE_NAMES:
        return SPEED_TEST_MIN_MBPS + 1  # CN-verified: skip download probe
    best = 0.0
    for _attempt in range(max(1, SPEED_TEST_ATTEMPTS)):
        mbps = download_speed_probe(proxy_port)
        if mbps is None:
            continue
        best = max(best, mbps)
        if best >= SPEED_TEST_MIN_MBPS * 4:
            break
    return best


def run_speed_tests(
    controller_url: str,
    proxy_port: int,
    metrics: list[ProxyMetric],
) -> list[ProxyMetric]:
    """Gate top-N low-latency nodes through a real download probe.

    Nodes that fail the minimum throughput threshold are dropped unless the
    surviving pool would fall below SPEED_TEST_MIN_POOL (degraded keep).
    """
    if not metrics:
        return metrics
    candidates = sorted(metrics, key=lambda item: (item.latency, -item.health_score))
    candidates = candidates[: SPEED_TEST_TOP_N]
    candidate_names = {str(item.proxy["name"]) for item in candidates}
    print(f"[INFO] speed testing top {len(candidates)} nodes via {SPEED_TEST_URL}")

    # Optional second egress stage. Disabled by default: the mihomo delay
    # probe above already performs a real HTTP fetch *through* the node, so
    # this stage re-proved the same thing serially at 12s per node — 30
    # minutes of the run for zero extra information, and the single biggest
    # reason a run could not finish before the next cron tick.
    if URL_TEST_STAGE:
        url_pass = 0
        for metric in candidates:
            name = str(metric.proxy["name"])
            if is_cn_alive(metric.proxy):
                metric.url_test_ok = True
                metric.speed_mbps = max(metric.speed_mbps, SPEED_TEST_MIN_MBPS + 1)
                url_pass += 1
                continue
            if not switch_benchmark_node(controller_url, name):
                continue
            try:
                resp = requests.get(
                    URL_TEST_URL,
                    proxies={
                        "http": f"http://127.0.0.1:{proxy_port}",
                        "https": f"http://127.0.0.1:{proxy_port}",
                    },
                    timeout=12,
                    headers={"User-Agent": f"free-proxy-airport/{VERSION}"},
                )
                metric.url_test_ok = resp.status_code == 200 and len(resp.content) > 100
            except Exception:
                metric.url_test_ok = False
            if metric.url_test_ok:
                url_pass += 1
        print(f"[INFO] url-test: {url_pass}/{len(candidates)} passed real HTTP egress")
        candidates = [m for m in candidates if m.url_test_ok]
        candidate_names = {str(item.proxy["name"]) for item in candidates}
    else:
        # The delay probe already succeeded for every candidate, so treat it
        # as the egress proof and keep the whole gate consistent.
        for metric in candidates:
            metric.url_test_ok = True
        print(f"[INFO] url-test stage skipped (delay probes already prove egress): {len(candidates)} candidates")

    for index, metric in enumerate(candidates, start=1):
        name = str(metric.proxy["name"])
        mbps = measure_speed_mbps(controller_url, proxy_port, metric.proxy)
        metric.speed_mbps = round(mbps, 2)
        status = "PASS" if mbps >= SPEED_TEST_MIN_MBPS else "SLOW"
        print(f"[SPEED] {index}/{len(candidates)} {name} {metric.speed_mbps}Mbps {status}")

    passed = [m for m in metrics if m.speed_mbps >= SPEED_TEST_MIN_MBPS and m.url_test_ok]
    dropped = len([m for m in candidates if 0 < m.speed_mbps < SPEED_TEST_MIN_MBPS])
    url_failed = len([m for m in candidates if not m.url_test_ok])
    if len(passed) < SPEED_TEST_MIN_POOL:
        keep = SPEED_TEST_MIN_POOL - len(passed)
        rescued = sorted(
            [m for m in candidates if m.speed_mbps < SPEED_TEST_MIN_MBPS or not m.url_test_ok],
            key=lambda item: item.speed_mbps,
            reverse=True,
        )[:keep]
        print(f"[WARN] pool below {SPEED_TEST_MIN_POOL}, keeping {len(rescued)} slow nodes as degraded fallback")
        passed.extend(rescued)
    untouched = [m for m in metrics if str(m.proxy["name"]) not in candidate_names]
    print(
        f"[INFO] speed gate: passed={len(passed)} slow_dropped={dropped} "
        f"url_failed={url_failed} untested_kept={len(untouched)}"
    )
    result = passed + untouched
    result.sort(key=publish_sort_key)
    return result


def detect_region(name: str) -> str:
    text = name.lower()
    patterns = {
        "HK": (
            "regex:\\bhk\\b",
            "hong kong",
            "\\u9999\\u6e2f",
            "\U0001f1ed\U0001f1f0",
        ),
        "JP": (
            "regex:\\bjp\\b",
            "japan",
            "\\u65e5\\u672c",
            "\U0001f1ef\U0001f1f5",
        ),
        "US": (
            "regex:\\bus\\b",
            "regex:\\busa\\b",
            "united states",
            "america",
            "\\u7f8e\\u56fd",
            "\\u7f8e\\u570b",
            "\U0001f1fa\U0001f1f8",
        ),
        "SG": (
            "regex:\\bsg\\b",
            "singapore",
            "\\u65b0\\u52a0\\u5761",
            "\U0001f1f8\U0001f1ec",
        ),
    }
    for region, tokens in patterns.items():
        for token in tokens:
            if token.startswith("regex:"):
                if re.search(token.removeprefix("regex:"), text):
                    return region
                continue
            if token.startswith("\\u"):
                token = token.encode("utf-8").decode("unicode_escape")
            if token in text:
                return region
    return "OTHER"


def region_bonus(region: str) -> int:
    if region in {"HK", "SG", "JP"}:
        return 3
    if region == "US":
        return 2
    return 1


def speed_bonus(speed_mbps: float) -> float:
    """Map measured Mbps onto a 0..1 score with a soft cap at 10 Mbps."""
    if speed_mbps <= 0:
        return 0.0
    return min(1.0, math.log10(1 + speed_mbps) / math.log10(1 + 10))


def health_score(name: str, latency: int, region: str, speed_mbps: float = 0.0) -> float:
    latency_score = 1.0 / latency if latency > 0 else 0.0
    latency_norm = latency_score * LATENCY_TIMEOUT_MS / 1000  # 1/latency scaled to ~[0.2, 1]
    return latency_norm * 0.4 + speed_bonus(speed_mbps) * 0.4 + region_bonus(region) / 3 * 0.2


def low_latency_pool(metrics: list[ProxyMetric]) -> list[str]:
    if not metrics:
        return ["DIRECT"]
    ordered = sorted(metrics, key=lambda item: (item.latency, -item.health_score))
    size = min(max(3, len(ordered) // 5), 30, len(ordered))
    return [item.proxy["name"] for item in ordered[:size]]


def names_for_region(metrics: list[ProxyMetric], region: str) -> list[str]:
    names = [item.proxy["name"] for item in metrics if item.region == region]
    if names:
        return names
    if metrics:
        return [item.proxy["name"] for item in metrics[: min(5, len(metrics))]]
    return ["DIRECT"]


def build_direct_fallback_metric() -> ProxyMetric:
    proxy = {"name": "DIRECT-FALLBACK", "type": "direct", "udp": True}
    return ProxyMetric(proxy=proxy, latency=LATENCY_TIMEOUT_MS, region="OTHER", health_score=0.0)


def load_existing_metrics() -> list[ProxyMetric]:
    if not OUTPUT_PATH.exists():
        return []
    try:
        data = yaml.safe_load(OUTPUT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, dict) or not isinstance(data.get("proxies"), list):
        return []
    metrics: list[ProxyMetric] = []
    for proxy in data["proxies"]:
        if not isinstance(proxy, dict):
            continue
        # Republishing a frozen config is the one path that bypasses
        # normalize_proxy(); keep the same server sanity gate here so a
        # loopback node in an old file cannot survive a "keep last good" run.
        if is_non_routable_server(proxy.get("server")):
            continue
        name = str(proxy.get("name", ""))
        region = detect_region(name)
        metrics.append(
            ProxyMetric(
                proxy=dict(proxy),
                latency=LATENCY_TIMEOUT_MS,
                region=region,
                health_score=health_score(name, LATENCY_TIMEOUT_MS, region, 0.0),
            )
        )
    return metrics


def url_test_group(name: str, proxies: list[str], interval: int = 120) -> dict[str, Any]:
    """url-test group tuned for a pool of free nodes.

    tolerance: without it url-test re-elects on every 1ms wobble, so the exit
    IP changes mid-session and logins/AI endpoints break - users read that as
    "the node is unstable".
    lazy: probe on load instead of only after the first proxied request, so
    the client shows real latency immediately instead of "testing...".
    """
    return {
        "name": name,
        "type": "url-test",
        "proxies": proxies,
        "url": TEST_URL,
        "interval": interval,
        "tolerance": 50,
        "lazy": False,
        "max-failed-times": 3,
        "timeout": 5000,
    }


def build_config(metrics: list[ProxyMetric]) -> dict[str, Any]:
    if not metrics:
        metrics = [build_direct_fallback_metric()]

    proxies = [item.proxy for item in metrics]
    all_names = [item.proxy["name"] for item in metrics]
    hk_names = names_for_region(metrics, "HK")
    jp_names = names_for_region(metrics, "JP")
    us_names = names_for_region(metrics, "US")
    ai_names = low_latency_pool(metrics)

    return {
        "mixed-port": 7890,
        # Security: an open LAN proxy lets anyone on your network use this
        # machine as their exit node. Keep it bound to localhost unless the
        # user consciously edits this after import.
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "ipv6": True,
        "unified-delay": True,
        "tcp-concurrent": True,
        "global-client-fingerprint": "chrome",
        "generated-by": f"free-proxy-airport-{VERSION}",
        "generated-at": datetime.now(timezone.utc).isoformat(),
        "proxies": proxies,
        "proxy-groups": [
            url_test_group("AUTO-FAST", all_names),
            url_test_group("HK-POOL", hk_names),
            url_test_group("JP-POOL", jp_names),
            url_test_group("US-POOL", us_names),
            url_test_group("AI-POOL", ai_names, interval=180),
            {
                "name": "FALLBACK",
                "type": "fallback",
                "proxies": ["AUTO-FAST", "HK-POOL", "JP-POOL", "US-POOL"],
                "url": TEST_URL,
                "interval": 120,
                "lazy": False,
            },
            {
                "name": "PROXY",
                "type": "select",
                # DIRECT last: when every free node is red the user can still
                # get online instead of the client showing a dead config.
                "proxies": ["AUTO-FAST", "FALLBACK", "DIRECT"],
            },
        ],
        "rules": [
            "DOMAIN-SUFFIX,openai.com,AI-POOL",
            "DOMAIN-SUFFIX,chatgpt.com,AI-POOL",
            "DOMAIN-SUFFIX,claude.ai,AI-POOL",
            "DOMAIN-SUFFIX,anthropic.com,AI-POOL",
            "GEOIP,CN,DIRECT",
            "MATCH,PROXY",
        ],
    }


def write_config(config: dict[str, Any]) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Write raw BYTES: str.write_text on Windows may route through a locale
    # encoder in some environments and double-encode CJK (mojibake). Encoding
    # to UTF-8 bytes explicitly keeps node names intact everywhere.
    payload = yaml.safe_dump(config, allow_unicode=True, sort_keys=False).encode("utf-8")
    OUTPUT_PATH.write_bytes(payload)




_CN_ALIVE_CACHE: dict[str, dict[str, Any]] | None = None
_CN_ALIVE_NAMES: set[str] = set()
# probed_at of the loaded NAS verdict; drives the freshness demotion.
_CN_ALIVE_TS: float = 0.0


def cn_verdict_is_fresh() -> bool:
    if not _CN_ALIVE_TS:
        return False
    return (time.time() - _CN_ALIVE_TS) <= CN_ALIVE_FRESH_SECONDS


def get_cn_alive_cached() -> dict[str, dict[str, Any]]:
    """Load cn_alive once per run; gates consult it to protect CN-verified nodes."""
    global _CN_ALIVE_CACHE
    if _CN_ALIVE_CACHE is None:
        _CN_ALIVE_CACHE = load_cn_alive()
    return _CN_ALIVE_CACHE


def is_cn_alive(proxy: dict[str, Any]) -> bool:
    fp = proxy_fingerprint(proxy)[:16]
    return fp in get_cn_alive_cached()


def save_candidates(metrics: list[ProxyMetric], total_nodes: int, sources_ok: int) -> None:
    """Persist cloud-verified candidates for the NAS phase."""
    CANDIDATES_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": int(time.time()),
        "total_nodes": total_nodes,
        "sources_ok": sources_ok,
        "candidates": [
            {
                "proxy": m.proxy,
                "latency": m.latency,
                "region": m.region,
                "speed_mbps": m.speed_mbps,
                "health_score": round(m.health_score, 6),
            }
            for m in metrics
        ],
    }
    CANDIDATES_FILE.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[SUMMARY] candidates_saved={len(metrics)} -> {CANDIDATES_FILE}")


def load_candidates() -> tuple[list[ProxyMetric], int, int] | None:
    """Load cloud-verified candidates saved by the collection run."""
    if not CANDIDATES_FILE.exists():
        return None
    try:
        payload = json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[WARN] candidates.json unreadable: {exc}")
        return None
    age = time.time() - payload.get("saved_at", 0)
    if age > CANDIDATES_TTL_SECONDS:
        print(f"[INFO] candidates.json stale ({age/3600:.1f}h > TTL), ignoring")
        return None
    metrics = [
        ProxyMetric(
            proxy=c["proxy"],
            latency=c["latency"],
            region=c["region"],
            health_score=c["health_score"],
            speed_mbps=c.get("speed_mbps", 0.0),
            url_test_ok=True,
        )
        for c in payload.get("candidates", [])
        if isinstance(c.get("proxy"), dict)
        and not is_non_routable_server(c["proxy"].get("server"))
    ]
    print(f"[INFO] loaded {len(metrics)} cloud-verified candidates (age {age/60:.0f}min)")
    return metrics, payload.get("total_nodes", 0), payload.get("sources_ok", 0)


def publish_sort_key(metric: ProxyMetric) -> tuple[int, float]:
    """CN-verified first, then by health score.

    Ordering is a tier, not a score bonus: +1000 on health_score guaranteed
    that whatever the NAS had verified survived every 400-node cap, so newly
    collected nodes never reached clients and the published set looked frozen
    even when the whole pipeline had run.
    """
    return (0 if metric.cn_verified else 1, -metric.health_score)


def publish(metrics: list[ProxyMetric], total_nodes: int, candidates: int) -> None:
    # Last-line defence: every ingress above filters non-routable servers,
    # but a future path could bypass them. Never write a loopback/literal-
    # private endpoint into the public subscription.
    before = len(metrics)
    metrics = [m for m in metrics if not is_non_routable_server(m.proxy.get("server"))]
    if len(metrics) != before:
        print(f"[INFO] publish filter: dropped {before - len(metrics)} non-routable node(s)")
    metrics = dedupe_published_by_endpoint(metrics)
    metrics.sort(key=publish_sort_key)
    if len(metrics) > MAX_OUTPUT_NODES:
        print(f"[INFO] capping {len(metrics)} -> {MAX_OUTPUT_NODES}")
        metrics = metrics[:MAX_OUTPUT_NODES]
    config = build_config(metrics)
    validate_config(config)
    write_config(config)
    print_summary(total_nodes, candidates, metrics)


def write_probe_queue(candidates: list[dict[str, Any]]) -> None:
    """Emit the CN-probe queue for the NAS worker (data/probe_queue.json)."""
    queue_path = Path("data/probe_queue.json")
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    targets = []
    for proxy in candidates[:PROBE_QUEUE_LIMIT]:
        cred = str(proxy.get("uuid") or proxy.get("password") or "")
        targets.append(
            {
                "fp": proxy_fingerprint(proxy)[:16],
                "name": str(proxy.get("name", "")),
                "type": str(proxy.get("type", "")),
                "server": str(proxy.get("server", "")),
                "port": int(proxy.get("port", 0)),
            }
        )
    queue_path.write_text(
        json.dumps({"queued_at": datetime.now(timezone.utc).isoformat(), "targets": targets}, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"[SUMMARY] probe_queue={len(targets)} -> {queue_path}")


def load_cn_alive() -> dict[str, dict[str, Any]]:
    """Read NAS probe results (data/cn_alive.json), 6h max age.

    Also records when the verdict was produced so later stages can demote it
    from admission gate to hint once it stops being fresh.
    """
    global _CN_ALIVE_TS
    path = Path("data/cn_alive.json")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    probed_at = data.get("probed_at", 0)
    if time.time() - probed_at > 21600:
        print("[INFO] cn_alive results stale (>6h), ignoring")
        return {}
    _CN_ALIVE_TS = float(probed_at or 0)
    alive = {r.get("fp", ""): r for r in data.get("alive", []) if r.get("fp")}
    print(f"[INFO] cn_alive: {len(alive)} alive nodes reported at {probed_at}")
    return alive


def validate_config(config: dict[str, Any]) -> None:
    if not isinstance(config.get("proxies"), list) or not config["proxies"]:
        raise RuntimeError("generated config has no proxies")
    groups = config.get("proxy-groups", [])
    group_names = {group.get("name") for group in groups if isinstance(group, dict)}
    missing = [name for name in REQUIRED_GROUPS if name not in group_names]
    if missing:
        raise RuntimeError(f"generated config missing groups: {missing}")
    rules = config.get("rules", [])
    for rule in (
        "DOMAIN-SUFFIX,openai.com,AI-POOL",
        "DOMAIN-SUFFIX,chatgpt.com,AI-POOL",
        "DOMAIN-SUFFIX,claude.ai,AI-POOL",
        "DOMAIN-SUFFIX,anthropic.com,AI-POOL",
        "GEOIP,CN,DIRECT",
        "MATCH,PROXY",
    ):
        if rule not in rules:
            raise RuntimeError(f"generated config missing rule: {rule}")


def print_summary(total_nodes: int, candidates: int, metrics: list[ProxyMetric]) -> None:
    hk_count = sum(1 for item in metrics if item.region == "HK")
    jp_count = sum(1 for item in metrics if item.region == "JP")
    us_count = sum(1 for item in metrics if item.region == "US")
    speed_tested = [item for item in metrics if item.speed_mbps > 0]
    avg_latency = round(sum(item.latency for item in metrics) / len(metrics), 2) if metrics else 0
    avg_speed = (
        round(sum(item.speed_mbps for item in speed_tested) / len(speed_tested), 2)
        if speed_tested
        else 0
    )
    print(f"[SUMMARY] total_nodes={total_nodes}")
    print(f"[SUMMARY] legal_candidates={candidates}")
    print(f"[SUMMARY] passed_latency_test={len(metrics)}")
    print(f"[SUMMARY] speed_tested={len(speed_tested)} avg_speed_mbps={avg_speed}")
    print(f"[SUMMARY] region_HK={hk_count} region_JP={jp_count} region_US={us_count}")
    print(f"[SUMMARY] avg_latency_ms={avg_latency}")
    print(f"[SUMMARY] output={OUTPUT_PATH}")


def main() -> None:
    """Two-phase pipeline.

    Phase A (no fresh NAS verdict):  collect -> gates -> save candidates.json
                                     + probe queue. Publishing keeps the last
                                     good subscription untouched.
    Phase B (fresh NAS verdict):     load candidates.json -> merge NAS verdicts
                                     -> cap 400 -> publish.

    Deadlock this fixes: the NAS publishes data/cn_alive.json every ~45 min, so
    `has_nas_verdict` was ALWAYS true. Phase A therefore never ran again, the
    probe queue stayed frozen at 2026-11-01 15:59 for 8 days (the NAS kept
    re-probing the same dead endpoints), candidates.json was never committed by
    CI so Phase B always fell back to a full collection, and a full collection
    with the old 105-minute budget could never finish before the next cron
    tick. Force collection with FREE_PROXY_AIRPORT_FORCE_COLLECT=1.
    """
    cn_alive = load_cn_alive()
    has_nas_verdict = bool(cn_alive)
    force_collect = os.getenv("FREE_PROXY_AIRPORT_FORCE_COLLECT", "0") == "1"
    stale_verdict = has_nas_verdict and not cn_verdict_is_fresh()
    if stale_verdict:
        age_min = (time.time() - _CN_ALIVE_TS) / 60
        print(
            f"[INFO] NAS verdict is {age_min:.0f}min old (> {CN_ALIVE_FRESH_SECONDS//60}min): "
            "refreshing candidates instead of republishing the pinned set"
        )

    if not has_nas_verdict or force_collect or stale_verdict:
        # ---------------- PHASE A: collection run ----------------
        print("=== PHASE A: collection run ===")
        total_nodes, candidates, source_report = collect_proxies()
        ok_sources = sum(1 for _, ok in source_report if ok)
        print(f"[SUMMARY] sources_total={len(source_report)} sources_ok={ok_sources}")
        if source_report and ok_sources == 0:
            print("[WARN] ALL sources failed; check sources.txt URLs or your network egress")

        metrics: list[ProxyMetric] = []
        if candidates:
            try:
                metrics = benchmark_proxies(candidates)
            except Exception as exc:
                print(f"[WARN] benchmark unavailable: {exc}")

        if metrics:
            save_candidates(metrics, total_nodes, ok_sources)
            # full pool becomes the next probe queue
            try:
                write_probe_queue([m.proxy for m in metrics])
            except Exception as exc:
                print(f"[WARN] probe queue write failed: {exc}")
            if force_collect or stale_verdict:
                # Keep serving the freshly measured set instead of leaving a
                # days-old file up while waiting for the NAS round trip.
                print("[INFO] phase A: publishing measured set directly (verdict stale/forced)")
                merge_cn_verdicts(metrics, cn_alive)
                publish(metrics, total_nodes, len(candidates))
                return
            print("[INFO] phase A complete: candidates staged, subscription NOT republished")
            return

        # benchmark failed entirely -> keep last good output
        legacy = load_existing_metrics()
        if legacy:
            print("[WARN] benchmark failed; keeping last good subscription")
            publish(legacy, total_nodes, len(candidates))
        else:
            print("[WARN] nothing works; DIRECT-FALLBACK degraded config")
            publish([build_direct_fallback_metric()], 0, 0)
        return

    # ---------------- PHASE B: publish run ----------------
    print("=== PHASE B: publish run (fresh NAS verdict) ===")
    loaded = load_candidates()
    if not loaded:
        print("[WARN] no staged candidates; falling back to full collection+publish")
        total_nodes, candidates, _ = collect_proxies()
        metrics: list[ProxyMetric] = []
        if candidates:
            try:
                metrics = benchmark_proxies(candidates)
            except Exception:
                metrics = []
        if not metrics:
            legacy = load_existing_metrics()
            publish(legacy or [build_direct_fallback_metric()], total_nodes, len(candidates))
            return
        total_nodes, candidates_count = total_nodes, len(candidates)
        # The queue MUST be refreshed on this path too. Without it the NAS
        # keeps probing whatever it was queued with weeks ago, its verdicts
        # describe endpoints that no longer exist, and every publish keeps
        # shipping the same pinned handful of nodes.
        try:
            write_probe_queue([m.proxy for m in metrics])
        except Exception as exc:
            print(f"[WARN] probe queue write failed: {exc}")
    else:
        metrics, total_nodes, candidates_count = loaded

    merge_cn_verdicts(metrics, cn_alive)
    publish(metrics, total_nodes, candidates_count)


def merge_cn_verdicts(metrics: list[ProxyMetric], cn_alive: dict[str, dict[str, Any]]) -> None:
    """Mark CN-verified nodes as a ranking tier (never as a score bonus)."""
    boosted = 0
    for metric in metrics:
        if is_cn_alive(metric.proxy):
            metric.cn_verified = True
            metric.url_test_ok = True
            boosted += 1
    print(f"[INFO] NAS verdicts merged: {boosted}/{len(metrics)} CN-verified (tier-0)")
    if not boosted:
        print("[WARN] no candidate matched the NAS verdict set: queue/verdict fingerprints diverged")
    elif not cn_alive:
        print("[INFO] no NAS verdict available: ranking falls back to measured health score")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise

# CN-PROBE-LOOP-ACTIVE: queue emission enabled 2026-11-01
