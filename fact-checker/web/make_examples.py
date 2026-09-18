#!/usr/bin/env python3
"""Run the full claim check on the repo's sample essays and save them as sidebar examples.

    python web/make_examples.py                     # both essays
    python web/make_examples.py data/essays/x.md    # just this one

Each example is a real agent run, saved to data/runs/example-<name>/ and marked
"example": true, so every signed-in account sees it read-only in the history sidebar.
Re-run this after changing a prompt to refresh the examples. It spends API credit.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import server  # noqa: E402  (also puts pipeline/ on sys.path)
import steps  # noqa: E402
from lib import ROOT, write_json  # noqa: E402

ESSAYS = [ROOT / "data" / "essays" / "shumer_2026-02-09.md", ROOT / "data" / "essays" / "rebuttal_marcus.md"]


def make(essay: Path) -> None:
    run_id = "example-" + re.sub(r"[^A-Za-z0-9_-]", "-", essay.stem)
    run_dir = server.RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    text = essay.read_text(encoding="utf-8")
    (run_dir / f"article{essay.suffix}").write_text(text, encoding="utf-8")
    log = ROOT / "notes" / f"sources_{run_id}.log"
    log.unlink(missing_ok=True)

    print(f"{essay.name}: extract", flush=True)
    data = {"claims": steps.extract_claims(text)}
    print(f"  {len(data['claims'])} claims → classify", flush=True)
    data["classifications"] = steps.classify_claims(data["claims"])
    print("  → sources (slow)", flush=True)
    data["evidence"] = steps.find_sources(data["claims"], log)
    print("  → verdicts", flush=True)
    data["verdicts"] = steps.decide_verdicts(data["claims"], data["evidence"])

    for key, filename in server.FILE_FOR.items():
        write_json(run_dir / filename, {key: data[key]})
    meta = server.save_meta(
        run_dir,
        data,
        run_id=run_id,
        user=None,
        filename=essay.name,
        title=server.title_of(text, essay.name),
        words=len(text.split()),
        created_at=server.now_iso(),
        example=True,
    )
    print(f"  saved {run_dir} {meta['counts']}")


if __name__ == "__main__":
    for path in [Path(a) for a in sys.argv[1:]] or ESSAYS:
        make(path)
