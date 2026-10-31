from __future__ import annotations

import base64
import gzip
import hashlib
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


VERSION = "v7"
OUTPUT_PATH = Path("output/clash.yaml")
TEST_URL = "http://www.gstatic.com/generate_204"
SOURCE_TIMEOUT = 25
LATENCY_TIMEOUT_MS = 5000
MAX_RETRIES = 3
MAX_WORKERS = int(os.getenv("FREE_PROXY_AIRPORT_MAX_WORKERS", "24"))
MAX_CANDIDATES = int(os.getenv("FREE_PROXY_AIRPORT_MAX_CANDIDATES", "0"))
SPEED_TEST_URL = "https://speed.cloudflare.com/__down?bytes=25000000"
SPEED_TEST_MAX_BYTES = 10_000_000
SPEED_TEST_TIMEOUT = 25.0
SPEED_TEST_MIN_MBPS = float(os.getenv("FREE_PROXY_AIRPORT_MIN_MBPS", "1.0"))
SPEED_TEST_TOP_N = int(os.getenv("FREE_PROXY_AIRPORT_SPEED_TOP_N", "30"))
SPEED_TEST_MIN_POOL = int(os.getenv("FREE_PROXY_AIRPORT_MIN_POOL", "10"))

# One subscription URL per line. Lines starting with # are comments.
# Add a new free source -> append a line. A source that keeps failing -> delete its line.
SOURCES_FILE = Path(os.getenv("FREE_PROXY_AIRPORT_SOURCES", "sources.txt"))
DEFAULT_SOURCES = """\
# free-proxy-airport node sources: one URL per line, '#' starts a comment
# openRunner clash-freenode
https://raw.githubusercontent.com/openRunner/clash-freenode/main/sub.yaml
# snakem982 proxypool
https://raw.githubusercontent.com/snakem982/proxypool/main/clash.yaml
# Flikify Free-Node
https://raw.githubusercontent.com/Flikify/Free-Node/main/clash.yaml
# free-clash-v2ray GitHub Pages
https://free-clash-v2ray.github.io/uploads/latest.yaml
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
    "http",
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
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def maybe_base64_decode(text: str) -> str:
    # Strip BOM and zero-width chars that break base64 sniffing on free sources.
    cleaned = text.lstrip("﻿ \u200b\u200c\u200d").strip()
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


def _uri_fragment_name(uri: str) -> str:
    fragment = uri.split("#", 1)[1] if "#" in uri else ""
    return quote("", safe="") + fragment if False else fragment


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
        if query.get("security") == "tls" or proxy_type in ("trojan", "hysteria", "hysteria2", "tuic"):
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
    result: list[dict[str, Any]] = []

    for index, raw in enumerate(proxies, start=1):
        proxy = normalize_proxy(raw, index)
        if not proxy:
            continue

        fingerprint = proxy_fingerprint(proxy)
        if fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(fingerprint)

        base_name = str(proxy["name"]).strip() or f"node-{index}"
        name = base_name
        suffix = 2
        while name in seen_names:
            name = f"{base_name}-{suffix}"
            suffix += 1
        proxy["name"] = name
        seen_names.add(name)
        result.append(proxy)
    return result


def normalize_proxy(raw: dict[str, Any], index: int) -> dict[str, Any] | None:
    proxy = {key: value for key, value in raw.items() if value is not None}
    proxy_type = str(proxy.get("type", "")).lower().strip()
    if proxy_type not in SUPPORTED_PROXY_TYPES:
        return None

    if proxy_type == "hy2":
        proxy_type = "hysteria2"
    proxy["type"] = proxy_type

    name = str(proxy.get("name", "")).strip() or f"node-{index}"
    server = str(proxy.get("server", "")).strip()
    if not server:
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

    url = select_mihomo_asset()
    print(f"[INFO] downloading proxy engine: {url}")
    archive = download_file(url, install_dir)
    extracted = extract_mihomo_binary(archive, install_dir)
    extracted.chmod(extracted.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if extracted != binary:
        shutil.copy2(extracted, binary)
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return binary


def select_mihomo_asset() -> str:
    api_url = "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest"
    data = requests.get(api_url, headers={"User-Agent": "free-proxy-airport"}, timeout=SOURCE_TIMEOUT).json()
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

    candidates: list[tuple[int, str]] = []
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
        candidates.append((score, download_url))

    if not candidates:
        raise RuntimeError("no matching Mihomo release asset found")
    candidates.sort(reverse=True)
    return candidates[0][1]


def download_file(url: str, directory: Path) -> Path:
    target = directory / Path(url.split("?")[0]).name
    with requests.get(url, stream=True, timeout=SOURCE_TIMEOUT) as response:
        response.raise_for_status()
        with target.open("wb") as file:
            for chunk in response.iter_content(chunk_size=1024 * 512):
                if chunk:
                    file.write(chunk)
    return target


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
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return mixed_port


def wait_for_controller(controller_url: str, process: subprocess.Popen[str]) -> None:
    for _ in range(60):
        if process.poll() is not None:
            raise RuntimeError("Mihomo exited before controller became ready")
        try:
            response = requests.get(f"{controller_url}/version", timeout=1)
            if response.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("Mihomo controller did not become ready")


def benchmark_proxies(proxies: list[dict[str, Any]]) -> list[ProxyMetric]:
    if not proxies:
        return []

    engine = find_or_install_mihomo()
    with tempfile.TemporaryDirectory(prefix="free-proxy-airport-") as temp_name:
        temp_dir = Path(temp_name)
        config_path = temp_dir / "benchmark.yaml"
        controller_port = find_free_port()
        controller_url = f"http://127.0.0.1:{controller_port}"
        mixed_port = write_benchmark_config(config_path, proxies, controller_port)

        process = subprocess.Popen(
            [str(engine), "-d", str(temp_dir), "-f", str(config_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            wait_for_controller(controller_url, process)
            metrics = run_delay_tests(controller_url, proxies)
            if metrics:
                metrics = run_speed_tests(controller_url, mixed_port, metrics)
                for metric in metrics:
                    metric.health_score = health_score(
                        str(metric.proxy.get("name", "")),
                        metric.latency,
                        metric.region,
                        metric.speed_mbps,
                    )
                metrics.sort(key=lambda item: item.health_score, reverse=True)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        return metrics


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
    metrics.sort(key=lambda item: item.health_score, reverse=True)
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


def measure_speed_mbps(controller_url: str, proxy_port: int, name: str) -> float:
    """Best-of-2 single-connection download through one proxy node."""
    if not switch_benchmark_node(controller_url, name):
        return 0.0
    best = 0.0
    for attempt in range(2):
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

    for index, metric in enumerate(candidates, start=1):
        name = str(metric.proxy["name"])
        mbps = measure_speed_mbps(controller_url, proxy_port, name)
        metric.speed_mbps = round(mbps, 2)
        status = "PASS" if mbps >= SPEED_TEST_MIN_MBPS else "SLOW"
        print(f"[SPEED] {index}/{len(candidates)} {name} {metric.speed_mbps}Mbps {status}")

    passed = [m for m in metrics if m.speed_mbps >= SPEED_TEST_MIN_MBPS]
    dropped = len([m for m in candidates if 0 < m.speed_mbps < SPEED_TEST_MIN_MBPS])
    if len(passed) < SPEED_TEST_MIN_POOL:
        keep = SPEED_TEST_MIN_POOL - len(passed)
        rescued = sorted(
            [m for m in candidates if m.speed_mbps < SPEED_TEST_MIN_MBPS],
            key=lambda item: item.speed_mbps,
            reverse=True,
        )[:keep]
        print(f"[WARN] pool below {SPEED_TEST_MIN_POOL}, keeping {len(rescued)} slow nodes as degraded fallback")
        passed.extend(rescued)
    untouched = [m for m in metrics if str(m.proxy["name"]) not in candidate_names]
    print(f"[INFO] speed gate: passed={len(passed)} slow_dropped={dropped} untested_kept={len(untouched)}")
    result = passed + untouched
    result.sort(key=lambda item: item.health_score, reverse=True)
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
        "allow-lan": True,
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
            {
                "name": "AUTO-FAST",
                "type": "url-test",
                "proxies": all_names,
                "url": TEST_URL,
                "interval": 120,
            },
            {
                "name": "HK-POOL",
                "type": "url-test",
                "proxies": hk_names,
                "url": TEST_URL,
                "interval": 120,
            },
            {
                "name": "JP-POOL",
                "type": "url-test",
                "proxies": jp_names,
                "url": TEST_URL,
                "interval": 120,
            },
            {
                "name": "US-POOL",
                "type": "url-test",
                "proxies": us_names,
                "url": TEST_URL,
                "interval": 120,
            },
            {
                "name": "AI-POOL",
                "type": "url-test",
                "proxies": ai_names,
                "url": TEST_URL,
                "interval": 120,
            },
            {
                "name": "FALLBACK",
                "type": "fallback",
                "proxies": ["AUTO-FAST", "HK-POOL", "JP-POOL", "US-POOL"],
                "url": TEST_URL,
                "interval": 120,
            },
            {
                "name": "PROXY",
                "type": "select",
                "proxies": ["AUTO-FAST", "FALLBACK"],
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
    OUTPUT_PATH.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


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
            print(f"[WARN] real latency benchmark unavailable: {exc}")

    if not metrics:
        metrics = load_existing_metrics()
        if metrics:
            print("[WARN] no live nodes passed; reusing previous non-empty output as degraded fallback")

    if not metrics:
        metrics = [build_direct_fallback_metric()]
        print("[WARN] no live or previous nodes; using DIRECT-FALLBACK degraded config")

    metrics.sort(key=lambda item: item.health_score, reverse=True)
    config = build_config(metrics)
    validate_config(config)
    write_config(config)
    print_summary(total_nodes, len(candidates), metrics)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise
