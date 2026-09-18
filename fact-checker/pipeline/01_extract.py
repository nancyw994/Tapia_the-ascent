#!/usr/bin/env python3
"""Stage 1: essay → claims.json"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import chat, extract_json_array, load_prompt, make_client, write_json


def run(essay_path: Path, out_path: Path, model: str, base_url: str, max_chars: int = 12000) -> dict:
    essay = essay_path.read_text(encoding="utf-8")
    client = make_client(base_url)
    clipped = essay[:max_chars]  # small local models need a clip; API models can take the full essay
    for _ in range(3):  # retry a malformed reply instead of writing an empty claims.json
        items = extract_json_array(chat(client, model, load_prompt("extractor.md"), clipped, max_tokens=2200))
        claims = []
        for idx, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            quote = str(item.get("quote") or "").strip()
            if not quote or quote not in essay:
                continue
            loc = essay.find(quote)
            claims.append(
                {
                    "id": str(item.get("id") or f"c{idx:02d}"),
                    "quote": quote,
                    "taxonomy": str(item.get("taxonomy") or "event"),
                    "char_start": loc if loc >= 0 else None,
                }
            )
        if claims:
            break
    else:
        raise RuntimeError("extractor returned no claims whose quote appears verbatim in the essay")
    payload = {
        "essay_path": str(essay_path),
        "essay_id": essay_path.stem,
        "claims": claims,
    }
    write_json(out_path, payload)
    print(f"extract: {len(claims)} claims → {out_path}")
    return payload


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--essay", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--model", default="qwen2.5:3b")
    p.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    args = p.parse_args()
    run(args.essay, args.out, args.model, args.base_url)


if __name__ == "__main__":
    main()
