#!/usr/bin/env python3
"""Stage 3: classified.json → evidence.json"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import (
    append_source_log,
    assign_tier,
    chat,
    extract_json_object,
    host,
    load_prompt,
    make_client,
    read_json,
    search_web,
    write_json,
)


def queries_for(client, model: str, claim: dict) -> list[str]:
    data = extract_json_object(
        chat(
            client,
            model,
            load_prompt("searcher.md"),
            json.dumps({"quote": claim.get("quote")}, ensure_ascii=False),
            max_tokens=200,
        )
    )
    queries = [str(q).strip() for q in (data.get("queries") or []) if str(q).strip()]
    if not queries:
        queries = [claim.get("quote", "")[:120]]
    return queries[:2]


def run(
    classified_path: Path,
    out_path: Path,
    log_path: Path,
    model: str,
    base_url: str,
    max_verify: int,
    essay_url: str,
) -> dict:
    payload = read_json(classified_path)
    client = make_client(base_url)
    evidence = []
    verified = 0
    for claim in payload.get("claims") or []:
        row = {
            "id": claim["id"],
            "quote": claim.get("quote"),
            "label": claim.get("label"),
            "queries": [],
            "hits": [],
        }
        if claim.get("proceeds_to_search") and verified < max_verify:
            qs = queries_for(client, model, claim)
            row["queries"] = qs
            seen = set()
            hits = []
            for query in qs:
                batch = search_web(query)
                append_source_log(log_path, claim["id"], query, batch)
                for hit in batch:
                    url = hit.get("url") or ""
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    hit = dict(hit)
                    hit["host"] = host(url)
                    hit["tier"] = assign_tier(hit, claim.get("quote") or "", essay_url)
                    hit["stance"] = "unknown"
                    hits.append(hit)
            row["hits"] = hits
            verified += 1
        evidence.append(row)
    out = {**payload, "evidence": evidence}
    write_json(out_path, out)
    print(f"search: {verified} claims queried → {out_path}")
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--classified", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--log", type=Path, required=True)
    p.add_argument("--model", default="qwen2.5:3b")
    p.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    p.add_argument("--max-verify", type=int, default=8)
    p.add_argument("--essay-url", default="")
    args = p.parse_args()
    run(
        args.classified,
        args.out,
        args.log,
        args.model,
        args.base_url,
        args.max_verify,
        args.essay_url,
    )


if __name__ == "__main__":
    main()
