"""Single source of truth for every filesystem path in this project.

Before this module existed each entry point resolved its own paths relative to
the *current working directory* (``Path("data/candidates.json")``). That works
only when you happen to run from the repo root, and it silently writes into the
wrong tree when you do not — ``python airport/generator.py`` from ``airport/``
would have created ``airport/output/clash.yaml``.

Everything below is anchored on this file's location, so any entry point works
from any directory:

    python -m airport.generator            # from repo root
    cd airport && python generator.py      # from the package dir

Layout (see documentation/structure.md for the full contract):

    <repo>/data/       committed state: candidates, registry, NAS verdicts
    <repo>/output/     generated subscription (git-tracked, CI compares it)
    <repo>/docs/       GitHub Pages source — the published subscription only
    <repo>/out/        local scratch, gitignored
    <repo>/.tools/     downloaded engines, gitignored
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
DOCS_DIR = ROOT / "docs"
OUT_DIR = ROOT / "out"
TOOLS_DIR = ROOT / ".tools"

# --- committed state -------------------------------------------------------
SOURCES_FILE = ROOT / "sources.txt"
CANDIDATES_FILE = DATA_DIR / "candidates.json"
SOURCE_STATS_FILE = DATA_DIR / "source_stats.json"
PROBE_QUEUE_FILE = DATA_DIR / "probe_queue.json"
CN_ALIVE_FILE = DATA_DIR / "cn_alive.json"
REGISTRY_FILE = DATA_DIR / "node_registry.json"

# --- products --------------------------------------------------------------
OUTPUT_PATH = OUTPUT_DIR / "clash.yaml"
PAGES_PATH = DOCS_DIR / "clash.yaml"

# --- local scratch ---------------------------------------------------------
VERIFY_REPORT = OUT_DIR / "verify_report.json"
RESCUE_REPORT = OUT_DIR / "rescue_report.json"


def ensure_parent(path: Path) -> Path:
    """mkdir -p the parent directory of `path` and return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
