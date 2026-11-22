"""Shared helpers for source discovery: fetch, decode, count nodes.

Networking note: this module deliberately uses `requests` with
`trust_env=False`. From CN the local sing-box client (nekobox) sets a system
proxy that answers every foreign request with a synthetic 502 in ~5s, which
looks exactly like "all mirrors are dead". Bypassing the env proxy and dialing
out directly is what makes ghproxy.net work.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter

try:
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:  # noqa: BLE001
    pass

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) free-proxy-airport-discover/1.0"

PROTO_SCHEMES = (
    "vmess", "vless", "trojan", "ss", "ssr", "hysteria2", "hysteria",
    "tuic", "socks5", "socks", "anytls", "snell", "wireguard", "ssh",
)
SCHEME_RE = re.compile(r"\b(?:" + "|".join(PROTO_SCHEMES) + r")://[^\s\"'<>\\]+", re.I)

# raw.githubusercontent.com is TCP-reset by the GFW from CN. ghproxy.net over
# PLAIN HTTP is the one mirror that answers reliably (verified 2026-11-22:
# 518928 and 108064 bytes). Empty entry = direct, which is what GitHub Actions
# runners outside CN will use.
RAW_MIRRORS = [
    "",
    "http://ghproxy.net/",
]

_local = __import__("threading").local()


def session() -> requests.Session:
    """One Session per thread.

    A single shared Session under 64-way concurrency made ghproxy.net answer
    503 for half the requests (measured: 32/64 ok), because the mirror throttles
    per TCP connection and the shared pool reuses a few connections. A
    thread-local session gives each worker its own connection.
    """
    s = getattr(_local, "sess", None)
    if s is None:
        s = requests.Session()
        s.trust_env = False  # never inherit nekobox's system proxy
        s.verify = False
        ad = HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=0)
        s.mount("http://", ad)
        s.mount("https://", ad)
        s.headers.update({"User-Agent": UA, "Accept": "*/*"})
        _local.sess = s
    return s


def http_get(url: str, timeout: float = 20.0, retries: int = 2,
             headers: dict | None = None) -> tuple[int, bytes, str]:
    """Return (status, body, final_url). status 0 means transport failure.

    A single float timeout applies to connect AND read, so a slow mirror can
    hold a request for timeout*2. Splitting them (4s connect / read) cut a
    dead mirror response from 26s to 2s in measurement.
    """
    s = session()
    if isinstance(timeout, (int, float)):
        rt = (min(4.0, float(timeout)), float(timeout))
    else:
        rt = timeout
    last = (0, b"", url)
    for attempt in range(retries + 1):
        try:
            r = s.get(url, timeout=rt, headers=headers, allow_redirects=True)
            return r.status_code, r.content, r.url
        except Exception as e:  # noqa: BLE001
            last = (0, f"{type(e).__name__}: {e}".encode(), url)
            if attempt < retries:
                time.sleep(0.4 + attempt * 0.4)
    return last


def raw_url(full_name: str, branch: str, path: str, mirror: str = "") -> str:
    base = f"https://raw.githubusercontent.com/{full_name}/{branch}/{path}"
    return f"{mirror}{base}" if mirror else base


def fetch_raw(full_name: str, branch: str, path: str, timeout: float = 10.0):
    """Fetch a repo file, falling back through the mirror list."""
    last = (0, b"", "")
    for mirror in RAW_MIRRORS:
        url = raw_url(full_name, branch, path, mirror)
        status, body, _ = http_get_hard(url, hard_timeout=timeout)
        if status == 200 and len(body) > 100:
            return status, body, url
        last = (status, body, url)
        # A 200 with a short body, or a non-404 error, is worth one retry on
        # the next mirror; a clean 404 means the path simply does not exist.
    return last


_hard_pool = ThreadPoolExecutor(max_workers=128)


def http_get_hard(url: str, hard_timeout: float = 12.0,
                  headers: dict | None = None) -> tuple[int, bytes, str]:
    """http_get wrapped in a HARD wall-clock deadline.

    requests' read timeout only bounds a single socket read, so a mirror that
    trickles bytes can hold a call for 19s+ despite timeout=5 (measured on
    ghproxy.net 404s). A future.result(timeout=...) is the only reliable cap.
    """
    # retries=2 is required: ghproxy.net answers 503 under load and a single
    # attempt silently loses the file (this is why a 400-repo scan reported
    # 0 hits while the same URL worked in isolation).
    fut = _hard_pool.submit(http_get, url, hard_timeout, 2, headers)
    try:
        return fut.result(timeout=hard_timeout)
    except Exception:  # noqa: BLE001  (TimeoutError and friends)
        fut.cancel()
        return 0, b"hard-timeout", url


def b64_try_decode(body: bytes) -> bytes:
    """Best-effort base64 decode (subscription payloads)."""
    txt = body.strip()
    if txt.startswith(b"\xef\xbb\xbf"):
        txt = txt[3:]
    if b"://" in txt[:4096]:
        return txt
    compact = re.sub(rb"[^A-Za-z0-9+/=_-]", b"", txt)
    if len(compact) < 16:
        return txt
    for alt in (compact, compact + b"=" * (-len(compact) % 4)):
        try:
            dec = base64.urlsafe_b64decode(alt)
        except (binascii.Error, ValueError):
            try:
                dec = base64.b64decode(alt, validate=False)
            except Exception:  # noqa: BLE001
                continue
        if b"://" in dec:
            return dec
    return txt


def count_nodes(body: bytes) -> tuple[int, dict[str, int]]:
    """Count proxy nodes in a subscription payload -> (total, {type: n})."""
    dec = b64_try_decode(body)
    text = dec.decode("utf-8", "replace")
    links = SCHEME_RE.findall(text)
    kinds: dict[str, int] = {}
    for p in links:
        k = p.split("://", 1)[0].lower()
        kinds[k] = kinds.get(k, 0) + 1
    if not links and re.search(r"^\s*proxies\s*:", text, re.M):
        try:
            import yaml  # noqa: PLC0415

            data = yaml.safe_load(text)
            if isinstance(data, dict) and isinstance(data.get("proxies"), list):
                for p in data["proxies"]:
                    if isinstance(p, dict):
                        k = str(p.get("type", "?")).lower()
                        kinds[k] = kinds.get(k, 0) + 1
                return len(data["proxies"]), kinds
        except Exception:  # noqa: BLE001
            pass
    return len(links), kinds


def tcp_alive(host: str, port: int, timeout: float = 4.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def parallel(items, fn, workers: int = 32):
    out = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, it): it for it in items}
        for f in as_completed(futs):
            try:
                out.append((futs[f], f.result()))
            except Exception as e:  # noqa: BLE001
                out.append((futs[f], e))
    return out


def dump_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
