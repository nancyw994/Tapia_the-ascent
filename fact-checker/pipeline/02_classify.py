#!/usr/bin/env python3
"""Stage 2: claims.json → classified.json (only verifiable proceeds later)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import chat, extract_json_array, load_prompt, make_client, normalize_label, read_json, write_json


def run(claims_path: Path, out_path: Path, model: str, base_url: str) -> dict:
    payload = read_json(claims_path)
    client = make_client(base_url)
    expected = {str(c["id"]) for c in payload.get("claims") or []}
    for attempt in range(3):
        raw = chat(
            client,
            model,
            load_prompt("classifier.md"),
            json.dumps(payload.get("claims") or [], ensure_ascii=False)[:8000],
            max_tokens=1200,
        )
        by_id = {str(item.get("id")): item for item in extract_json_array(raw) if isinstance(item, dict)}
        if expected <= by_id.keys():
            break
    else:
        # Falling back to taxonomy here would silently label every claim "opinion" and skip search.
        raise RuntimeError(f"classifier gave no usable labels after 3 tries; last reply: {raw[:200]!r}")
    classified = []
    for claim in payload.get("claims") or []:
        row = dict(claim)
        guessed = by_id.get(claim["id"], {})
        label = normalize_label(str(guessed.get("label") or claim.get("taxonomy") or "opinion"))
        if claim.get("taxonomy") in {"anecdote", "rhetoric", "forecast"}:
            label = "opinion"
        row["label"] = label
        row["proceeds_to_search"] = label == "verifiable"
        classified.append(row)
    out = {**payload, "claims": classified}
    write_json(out_path, out)
    n = sum(1 for c in classified if c["proceeds_to_search"])
    print(f"classify: {n} verifiable / {len(classified)} → {out_path}")
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--claims", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--model", default="qwen2.5:3b")
    p.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    args = p.parse_args()
    run(args.claims, args.out, args.model, args.base_url)


if __name__ == "__main__":
    main()
