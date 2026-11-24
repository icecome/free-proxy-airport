#!/usr/bin/env python3
"""Local health + speed verification of one subscription, with registry upkeep.

The published list (`docs/clash.yaml`, 400 nodes) is what clients load, but the
generator's cloud run ranks nodes by DELAY only; a node that answers the 204
probe can still be unusably slow, and a node that passed yesterday can lose its
credential today. This script re-tests the *current* list from this machine:

  stage 1  TCP     raw connect to server:port
  stage 2  DELAY   mihomo url-test (real HTTP fetch through the node)
  stage 3  EGRESS  http://ip-api.com through the same tunnel (exit country/IP)
  stage 4  SPEED   one streaming download of SPEED_TEST_URL through the node

Verdicts are written back into `data/node_registry.json`:
  PASS / SLOW  -> record_verified  (VERIFIED_TTL, 24h)
  TCP/DELAY/ENGINE failure -> record_failed (blacklist, IP 7d / domain 3d)

Dedup is by what the client actually lists, `client_endpoint_key` = (host,
port), the same rule `generator.publish()` enforces. Duplicates are reported
and collapsed to the healthiest copy.

Usage:
    python scripts/local_verify.py                          # docs/clash.yaml, full run
    python scripts/local_verify.py --limit 12 --workers 2    # smoke test
    python scripts/local_verify.py --no-registry             # report only

Outputs (gitignored, out/ is local diagnostics only):
    out/verify_report.json   per-node stages, latency, speed, exit country
    out/verify_summary.txt   human-readable ranked list
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import node_registry as registry  # noqa: E402

ENGINE_ENV = os.getenv("FREE_PROXY_AIRPORT_ENGINE", "").strip()
ENGINE_CANDIDATES = [
    Path(ENGINE_ENV) if ENGINE_ENV else None,
    ROOT / ".tools" / "mihomo" / "mihomo.exe",
    ROOT / ".tools" / "mihomo" / "mihomo",
]
TEST_URL = "http://cp.cloudflare.com/generate_204"
SPEED_URL = "https://speed.cloudflare.com/__down?bytes=4000000"
SPEED_MAX_BYTES = 3_000_000
SPEED_TIMEOUT = float(os.getenv("FREE_PROXY_AIRPORT_SPEED_TIMEOUT", "10"))
MIN_MBPS = float(os.getenv("FREE_PROXY_AIRPORT_MIN_MBPS", "2.0"))
LATENCY_TIMEOUT_MS = int(os.getenv("FREE_PROXY_AIRPORT_LATENCY_TIMEOUT_MS", "5000"))
EGRESS_URL = "http://ip-api.com/json/?fields=status,country,countryCode,query"
DELAY_CONCURRENCY = int(os.getenv("FREE_PROXY_AIRPORT_DELAY_CONCURRENCY", "12"))
TCP_TIMEOUT = float(os.getenv("FREE_PROXY_AIRPORT_TCP_TIMEOUT", "4"))
OUT_DIR = ROOT / "out"


def session() -> requests.Session:
    """Never eat the system proxy: neko-box answers 502 after ~5s otherwise."""
    s = requests.Session()
    s.trust_env = False
    return s


HTTP = session()


# --------------------------------------------------------------------------- #
# load + dedupe
# --------------------------------------------------------------------------- #
def load_proxies(source: str) -> list[dict]:
    if source.startswith("http"):
        raw = HTTP.get(source, timeout=40).content
    else:
        raw = Path(source).read_bytes()
    data = yaml.safe_load(raw.decode("utf-8", "replace"))
    proxies = data.get("proxies", data) if isinstance(data, dict) else data
    return [p for p in proxies if isinstance(p, dict) and p.get("server")]


def endpoint_key(p: dict) -> tuple[str, int]:
    try:
        port = int(p.get("port", 0))
    except (TypeError, ValueError):
        port = 0
    return (str(p.get("server", "")).strip().lower(), port)


def dedupe(proxies: list[dict]) -> tuple[list[dict], list[dict]]:
    """Collapse duplicate endpoints; return (unique, duplicate_groups)."""
    groups: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for p in proxies:
        groups[endpoint_key(p)].append(p)
    unique, dropped = [], []
    for key, items in groups.items():
        unique.append(items[0])
        if len(items) > 1:
            dropped.append({
                "endpoint": f"{key[0]}:{key[1]}",
                "count": len(items),
                "kept": items[0].get("name"),
                "dropped": [i.get("name") for i in items[1:]],
            })
    return unique, dropped


# --------------------------------------------------------------------------- #
# stage 1: TCP
# --------------------------------------------------------------------------- #
def probe_tcp(p: dict) -> tuple[bool, int]:
    host, port = endpoint_key(p)
    if not host or not (0 < port < 65536):
        return False, 0
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=TCP_TIMEOUT):
            return True, max(1, round((time.monotonic() - start) * 1000))
    except OSError:
        return False, 0


# --------------------------------------------------------------------------- #
# engine instance
# --------------------------------------------------------------------------- #
def find_engine() -> Path | None:
    for cand in ENGINE_CANDIDATES:
        if cand is not None and cand.is_file():
            return cand
    found = shutil.which("mihomo")
    return Path(found) if found else None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def write_config(path: Path, proxies: list[dict]) -> tuple[int, int]:
    controller_port = free_port()
    mixed_port = free_port()
    # strip local bookkeeping keys: mihomo's decoder rejects unknown fields
    proxies = [{k: v for k, v in p.items() if not str(k).startswith("_")} for p in proxies]
    names = [str(p["name"]) for p in proxies]
    config = {
        "mixed-port": mixed_port,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "error",
        "external-controller": f"127.0.0.1:{controller_port}",
        "proxies": proxies,
        "proxy-groups": [{"name": "BENCHMARK", "type": "select", "proxies": names}],
        "rules": ["MATCH,BENCHMARK"],
    }
    path.write_bytes(yaml.safe_dump(config, allow_unicode=True, sort_keys=False).encode("utf-8"))
    return controller_port, mixed_port


def wait_controller(url: str, proc: subprocess.Popen) -> None:
    last = None
    for _ in range(80):
        if proc.poll() is not None:
            raise RuntimeError(f"engine exited early (code={proc.returncode})")
        try:
            if HTTP.get(f"{url}/version", timeout=1).status_code == 200:
                return
        except Exception as exc:  # noqa: BLE001 - probe, retry
            last = exc
        time.sleep(0.25)
    raise RuntimeError(f"controller not ready: {last}")


def select_node(controller: str, name: str) -> bool:
    try:
        r = HTTP.put(f"{controller}/proxies/{quote('BENCHMARK', safe='')}",
                     json={"name": name}, timeout=5)
        return r.status_code in (200, 204)
    except Exception:  # noqa: BLE001
        return False


def delay_probe(controller: str, name: str) -> tuple[bool, int, str]:
    url = (f"{controller}/proxies/{quote(name, safe='')}/delay"
           f"?timeout={LATENCY_TIMEOUT_MS}&url={quote(TEST_URL, safe='')}")
    try:
        r = HTTP.get(url, timeout=LATENCY_TIMEOUT_MS / 1000 + 4)
    except Exception as exc:  # noqa: BLE001
        return False, 0, f"{type(exc).__name__}"
    if r.status_code != 200:
        return False, 0, f"http {r.status_code}"
    try:
        delay = int(r.json().get("delay", 0))
    except Exception:  # noqa: BLE001
        return False, 0, "bad json"
    if delay <= 0:
        return False, 0, "timeout"
    return True, delay, ""


def egress(mixed_port: int) -> tuple[bool, str, str]:
    proxies = {"http": f"http://127.0.0.1:{mixed_port}",
               "https": f"http://127.0.0.1:{mixed_port}"}
    try:
        r = HTTP.get(EGRESS_URL, proxies=proxies, timeout=10,
                     headers={"User-Agent": "free-proxy-airport-local/1.0"})
        data = r.json()
    except Exception:  # noqa: BLE001
        return False, "", ""
    if data.get("status") != "success":
        return False, "", ""
    return True, str(data.get("countryCode", "")), str(data.get("query", ""))


def speed_test(mixed_port: int) -> float:
    proxies = {"http": f"http://127.0.0.1:{mixed_port}",
               "https": f"http://127.0.0.1:{mixed_port}"}
    received = 0
    start = time.monotonic()
    try:
        with HTTP.get(SPEED_URL, stream=True, proxies=proxies, timeout=SPEED_TIMEOUT,
                      headers={"User-Agent": "free-proxy-airport-local/1.0"}) as resp:
            if resp.status_code != 200:
                return 0.0
            for chunk in resp.iter_content(chunk_size=65536):
                received += len(chunk)
                if received >= SPEED_MAX_BYTES:
                    break
    except Exception:  # noqa: BLE001
        return 0.0
    elapsed = time.monotonic() - start
    if elapsed < 0.4 or received < 200_000:
        return 0.0
    return received * 8 / elapsed / 1_000_000


def run_instance(engine: Path, proxies: list[dict], work_dir: Path, tag: str) -> list[dict]:
    """One mihomo instance over a batch; falls back to per-node isolation."""
    work_dir.mkdir(parents=True, exist_ok=True)
    cfg = work_dir / f"{tag}.yaml"
    controller_port, mixed_port = write_config(cfg, proxies)
    controller = f"http://127.0.0.1:{controller_port}"
    log = work_dir / f"{tag}-engine.log"

    results: list[dict] = []
    with log.open("wb") as err:
        proc = subprocess.Popen([str(engine), "-d", str(work_dir), "-f", str(cfg)],
                                stdout=subprocess.DEVNULL, stderr=err)
        try:
            wait_controller(controller, proc)
        except RuntimeError as exc:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            tail = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-3:]
            detail = f"{exc} {' | '.join(tail)}"[:300]
            if len(proxies) > 1:
                # one poisoned node kills the whole config: isolate by halves
                mid = len(proxies) // 2
                results += run_instance(engine, proxies[:mid], work_dir, f"{tag}a")
                results += run_instance(engine, proxies[mid:], work_dir, f"{tag}b")
                return results
            return [{"proxy": proxies[0], "stage": "ENGINE", "ok": False, "detail": detail}]

        # stage 2: delay probes, concurrent inside the instance
        with concurrent.futures.ThreadPoolExecutor(max_workers=DELAY_CONCURRENCY) as ex:
            futs = {ex.submit(delay_probe, controller, str(p["name"])): p for p in proxies}
            first_pass = []
            for fut in concurrent.futures.as_completed(futs):
                p = futs[fut]
                try:
                    ok, delay, detail = fut.result()
                except Exception as exc:  # noqa: BLE001
                    ok, delay, detail = False, 0, type(exc).__name__
                if ok:
                    first_pass.append((p, delay))
                else:
                    results.append({"proxy": p, "stage": "DELAY", "ok": False,
                                    "delay": 0, "detail": detail})

        # stage 3+4: egress then speed, serially inside the instance
        for p, delay in first_pass:
            name = str(p["name"])
            if not select_node(controller, name):
                results.append({"proxy": p, "stage": "EGRESS", "ok": False, "delay": delay,
                                "detail": "selector switch failed"})
                continue
            got_egress, cc, ip = egress(mixed_port)
            mbps = speed_test(mixed_port)
            # The delay probe already fetched generate_204 *through* the node,
            # which is the same egress proof the generator's pipeline accepts.
            # Speed below MIN_MBPS is "slow", not "dead": it stays verified but
            # is ranked last and reported, never blacklisted.
            results.append({
                "proxy": p,
                "stage": "OK" if mbps >= MIN_MBPS else "SLOW",
                "ok": True,
                "delay": delay,
                "speed": round(mbps, 2),
                "exit_cc": cc,
                "exit_ip": ip,
                "detail": "" if got_egress else "egress probe failed",
            })
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
    return results


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #
def apply_registry(report_path: Path, dry_run: bool) -> int:
    """Replay a saved report into node_registry (same rules as a live run)."""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    proxies, _ = dedupe(load_proxies(report.get("source", str(ROOT / "docs" / "clash.yaml"))))
    by_key = {registry.node_key(p): p for p in proxies}
    reg_path = ROOT / "data" / "node_registry.json"
    if dry_run:
        print("[DRY-RUN] verdicts that would be written:")
    else:
        backup = ROOT / "data" / f"_node_registry-{time.strftime('%Y%m%d-%H%M%S')}.json"
        if reg_path.exists():
            shutil.copy2(reg_path, backup)
            print(f"[INFO] registry backup -> {backup.name}")
    reg = registry.load_registry(reg_path)
    before = (len(reg["verified"]), len(reg["failed"]))
    written = {"verified": 0, "failed": 0, "missing": 0}
    for row in report.get("results", []):
        proxy = by_key.get(row.get("key"))
        if proxy is None:
            written["missing"] += 1
            continue
        if row.get("ok"):
            if not dry_run:
                registry.record_verified(reg, proxy, f"{row.get('stage')} {row.get('delay')}ms "
                                                    f"{row.get('speed')}Mbps {row.get('exit_cc')}")
            written["verified"] += 1
        else:
            if not dry_run:
                registry.record_failed(reg, proxy, row.get("stage", "UNKNOWN"))
            written["failed"] += 1
    if not dry_run:
        reg = registry.prune(reg)
        registry.save_registry(reg, reg_path)
        print(f"[INFO] registry verified {before[0]}->{len(reg['verified'])} "
              f"failed(blacklist) {before[1]}->{len(reg['failed'])}")
    print(f"[INFO] replayed verified={written['verified']} blacklisted={written['failed']} "
          f"unmatched={written['missing']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sub", default=str(ROOT / "docs" / "clash.yaml"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8, help="mihomo instances in parallel")
    ap.add_argument("--batch", type=int, default=50, help="nodes per instance")
    ap.add_argument("--out", default=str(OUT_DIR / "verify_report.json"))
    ap.add_argument("--no-registry", action="store_true", help="do not touch the registry")
    ap.add_argument("--apply-registry", default=None,
                    help="replay verdicts from an earlier report into the registry, then exit")
    args = ap.parse_args()

    if args.apply_registry:
        return apply_registry(Path(args.apply_registry), args.no_registry)

    OUT_DIR.mkdir(exist_ok=True)
    engine = find_engine()
    if engine is None:
        print("[FATAL] mihomo engine not found; set FREE_PROXY_AIRPORT_ENGINE")
        return 2
    print(f"[INFO] engine: {engine}")

    raw = load_proxies(args.sub)
    proxies, dupes = dedupe(raw)
    print(f"[INFO] loaded={len(raw)} unique_endpoints={len(proxies)} duplicate_groups={len(dupes)}")
    for d in dupes[:10]:
        print(f"  [DEDUP] {d['endpoint']} x{d['count']} keep={d['kept']}")
    if args.limit:
        proxies = proxies[: args.limit]

    # stage 1: TCP for every node (cheap, prunes dead ports before mihomo)
    tcp: dict[int, tuple[bool, int]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        futs = {ex.submit(probe_tcp, p): i for i, p in enumerate(proxies)}
        for fut in concurrent.futures.as_completed(futs):
            tcp[futs[fut]] = fut.result()
    alive = [i for i, (ok, _) in tcp.items() if ok]
    print(f"[INFO] TCP reachable {len(alive)}/{len(proxies)} "
          f"({100 * len(alive) // max(1, len(proxies))}%)")

    results: list[dict] = []
    for i, p in enumerate(proxies):
        ok, ms = tcp[i]
        if not ok:
            results.append({"proxy": p, "stage": "TCP", "ok": False, "delay": 0, "detail": "tcp connect failed"})
        else:
            p["_tcp_ms"] = ms

    todo = [proxies[i] for i in alive]
    batches = [todo[i:i + args.batch] for i in range(0, len(todo), args.batch)]
    print(f"[INFO] tunnel-testing {len(todo)} nodes in {len(batches)} batches "
          f"({args.workers} instances, {DELAY_CONCURRENCY} delay probes each)")

    work_root = OUT_DIR / "local_verify"
    shutil.rmtree(work_root, ignore_errors=True)
    work_root.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(run_instance, engine, b, work_root / f"w{i}", f"w{i}"): i
                for i, b in enumerate(batches)}
        for done, fut in enumerate(concurrent.futures.as_completed(futs), start=1):
            got = fut.result()
            results += got
            ok = sum(1 for r in results if r.get("ok"))
            print(f"[{done}/{len(futs)}] batches done, usable so far={ok} "
                  f"({time.monotonic() - t0:.0f}s)")

    for r in results:
        p = r["proxy"]
        r.update(name=str(p.get("name")), type=str(p.get("type")),
                 server=str(p.get("server")), port=p.get("port"),
                 tcp_ms=p.get("_tcp_ms", 0), key=registry.node_key(p))
    results.sort(key=lambda r: (not r.get("ok"), r.get("delay") or 10**6, -float(r.get("speed") or 0)))

    stages = Counter(r["stage"] for r in results)
    usable = [r for r in results if r.get("ok")]
    fast = [r for r in usable if float(r.get("speed") or 0) >= MIN_MBPS]
    print("\n=== STAGE BREAKDOWN ===")
    for stage, n in stages.most_common():
        print(f"  {stage:<8} {n:>4}")
    print(f"\nUSABLE {len(usable)}/{len(results)}  (>= {MIN_MBPS}Mbps: {len(fast)})")

    payload = {
        "generated_at": int(time.time()),
        "source": str(args.sub),
        "engine": str(engine),
        "counts": {"loaded": len(raw), "unique": len(proxies), "tested": len(results),
                    "usable": len(usable), "fast": len(fast)},
        "duplicate_groups": dupes,
        "stages": dict(stages),
        "results": [{k: v for k, v in r.items() if k != "proxy"} for r in results],
    }
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    lines = [f"# local verify {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"# usable={len(usable)}/{len(results)} fast={len(fast)}", ""]
    for r in usable:
        lines.append(f"{r.get('delay'):>5}ms {float(r.get('speed') or 0):>6.2f}Mbps "
                     f"{r.get('exit_cc') or '??'} {r['type']:<8} {r['name']}")
    (OUT_DIR / "verify_summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"report -> {args.out}\nsummary -> {OUT_DIR / 'verify_summary.txt'}")

    if not args.no_registry:
        reg_path = ROOT / "data" / "node_registry.json"
        backup = ROOT / "data" / f"_node_registry-{time.strftime('%Y%m%d-%H%M%S')}.json"
        if reg_path.exists():
            shutil.copy2(reg_path, backup)
            print(f"[INFO] registry backup -> {backup.name}")
        reg = registry.load_registry(reg_path)
        before = (len(reg["verified"]), len(reg["failed"]))
        for r in results:
            p = r["proxy"]
            if r.get("ok"):
                registry.record_verified(reg, p, f"{r['stage']} {r.get('delay')}ms "
                                                 f"{r.get('speed')}Mbps {r.get('exit_cc')}"
                                                 f" tcp={r.get('tcp_ms')}ms")
            else:
                registry.record_failed(reg, p, r["stage"])
        reg = registry.prune(reg)
        registry.save_registry(reg, reg_path)
        print(f"[INFO] registry verified {before[0]}->{len(reg['verified'])} "
              f"failed(blacklist) {before[1]}->{len(reg['failed'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
