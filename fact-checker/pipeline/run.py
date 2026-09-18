#!/usr/bin/env python3
"""Run extract → classify → search → compare → annotate. Log every URL."""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = Path(__file__).resolve().parent
PY = sys.executable


def call(script: str, args: list[str]) -> None:
    cmd = [PY, str(PIPELINE / script), *args]
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--essay", type=Path, default=ROOT / "data/essays/shumer_2026-02-09.md")
    p.add_argument("--essay-url", default="https://shumer.dev/something-big-is-happening")
    p.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    p.add_argument("--model", default="qwen2.5:3b")
    p.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    p.add_argument("--max-verify", type=int, default=8)
    args = p.parse_args()

    run_dir = ROOT / "data" / "runs" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log = ROOT / "notes" / f"sources_{args.run_id}.log"
    common = ["--model", args.model, "--base-url", args.base_url]

    call("01_extract.py", ["--essay", str(args.essay), "--out", str(run_dir / "claims.json"), *common])
    call(
        "02_classify.py",
        ["--claims", str(run_dir / "claims.json"), "--out", str(run_dir / "classified.json"), *common],
    )
    call(
        "03_search.py",
        [
            "--classified",
            str(run_dir / "classified.json"),
            "--out",
            str(run_dir / "evidence.json"),
            "--log",
            str(log),
            "--max-verify",
            str(args.max_verify),
            "--essay-url",
            args.essay_url,
            *common,
        ],
    )
    call(
        "04_compare.py",
        [
            "--evidence",
            str(run_dir / "evidence.json"),
            "--out",
            str(run_dir / "verdicts.json"),
            "--essay-url",
            args.essay_url,
            *common,
        ],
    )
    call(
        "05_annotate.py",
        [
            "--essay",
            str(args.essay),
            "--verdicts",
            str(run_dir / "verdicts.json"),
            "--html",
            str(run_dir / "annotated.html"),
            "--md",
            str(run_dir / "annotated.md"),
        ],
    )
    print(f"run complete: {run_dir}")
    print(f"source log: {log}")


if __name__ == "__main__":
    main()
