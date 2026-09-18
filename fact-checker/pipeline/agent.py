#!/usr/bin/env python3
"""Agent entry point: start() reads the essay and runs the whole fact-check via OpenRouter.

Writes, in order, into data/runs/<run_id>/:
claims.json → classified.json → evidence.json → verdicts.json → verifier_challenges.json,
then renders annotated.html / annotated.md from verdicts.json.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

PIPELINE = Path(__file__).resolve().parent
ROOT = PIPELINE.parent
sys.path.insert(0, str(PIPELINE))
sys.path.insert(0, str(ROOT / "verify"))

from lib import API_BACKEND, read_json  # noqa: E402
from llm_client import DEFAULT_MODEL  # noqa: E402  (lib.py puts scripts/ on sys.path)

DEFAULT_ESSAY = ROOT / "data" / "essays" / "shumer_2026-02-09.md"
DEFAULT_ESSAY_URL = "https://shumer.dev/something-big-is-happening"
RUNS = ROOT / "data" / "runs"

# (stage key, output file) in execution order; the server uses this to draw progress.
STAGES = [
    ("extract", "claims.json"),
    ("classify", "classified.json"),
    ("search", "evidence.json"),
    ("compare", "verdicts.json"),
    ("verify", "verifier_challenges.json"),
    ("annotate", "annotated.html"),
]

Progress = Callable[[str, str, str], None]  # (stage, "running" | "done" | "error", detail)


def _count(path: Path, key: str) -> int:
    return len(read_json(path).get(key) or [])


def start(
    essay_path: Path = DEFAULT_ESSAY,
    run_id: str | None = None,
    model: str = DEFAULT_MODEL,
    essay_url: str = DEFAULT_ESSAY_URL,
    max_verify: int = 8,
    on_progress: Progress | None = None,
) -> dict:
    """Run the pipeline end to end and return {"run_id", "run_dir", "files"}."""
    essay_path = Path(essay_path)
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log = ROOT / "notes" / f"sources_{run_id}.log"
    out = {name: run_dir / name for _, name in STAGES}
    llm = {"model": model, "base_url": API_BACKEND}

    extract = importlib.import_module("01_extract")
    classify = importlib.import_module("02_classify")
    search = importlib.import_module("03_search")
    compare = importlib.import_module("04_compare")
    annotate = importlib.import_module("05_annotate")
    verifier = importlib.import_module("verifier_agent")

    steps = {
        "extract": lambda: extract.run(essay_path, out["claims.json"], max_chars=200_000, **llm),
        "classify": lambda: classify.run(out["claims.json"], out["classified.json"], **llm),
        "search": lambda: search.run(
            out["classified.json"], out["evidence.json"], log, max_verify=max_verify, essay_url=essay_url, **llm
        ),
        "compare": lambda: compare.run(out["evidence.json"], out["verdicts.json"], essay_url=essay_url, **llm),
        "verify": lambda: verifier.run(
            out["verdicts.json"], out["evidence.json"], out["verifier_challenges.json"], **llm
        ),
        "annotate": lambda: annotate.run(
            essay_path, out["verdicts.json"], out["annotated.html"], run_dir / "annotated.md"
        ),
    }
    details = {
        "extract": lambda: f"{_count(out['claims.json'], 'claims')} claims",
        "classify": lambda: f"{sum(c['proceeds_to_search'] for c in read_json(out['classified.json'])['claims'])} verifiable",
        "search": lambda: f"{sum(bool(e['hits']) for e in read_json(out['evidence.json'])['evidence'])} claims with sources",
        "compare": lambda: f"{_count(out['verdicts.json'], 'claims')} verdicts",
        "verify": lambda: f"{_count(out['verifier_challenges.json'], 'challenges')} challenges",
        "annotate": lambda: "annotated.html",
    }

    notify = on_progress or (lambda stage, status, detail: print(f"[{stage}] {status} {detail}"))
    for stage, _ in STAGES:
        notify(stage, "running", "")
        try:
            steps[stage]()
        except Exception as exc:  # noqa: BLE001
            notify(stage, "error", f"{type(exc).__name__}: {exc}")
            raise
        notify(stage, "done", details[stage]())
    return {"run_id": run_id, "run_dir": str(run_dir), "files": {name: str(p) for name, p in out.items()}}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--essay", type=Path, default=DEFAULT_ESSAY)
    p.add_argument("--run-id")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--max-verify", type=int, default=8)
    args = p.parse_args()
    result = start(args.essay, args.run_id, args.model, max_verify=args.max_verify)
    print(f"run complete: {result['run_dir']}")


if __name__ == "__main__":
    main()
