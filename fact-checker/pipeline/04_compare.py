#!/usr/bin/env python3
"""Stage 4: evidence.json → verdicts.json"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import chat, extract_json_object, is_reprint, load_prompt, make_client, now_iso, read_json, write_json


def independent_hits(claim_quote: str, hits: list[dict], essay_url: str) -> list[dict]:
    kept = []
    seen_hosts = set()
    for hit in hits:
        if not hit.get("url"):
            continue
        if hit.get("tier") == 0 or is_reprint(hit, claim_quote, essay_url):
            continue
        h = hit.get("host") or hit.get("url")
        if h in seen_hosts:
            continue
        seen_hosts.add(h)
        kept.append(hit)
    return kept


def cap_confidence(verdict: str, confidence: float, independent: list, hits: list) -> float:
    if verdict == "supported" and len(independent) < 2:
        return min(confidence, 0.0)
    if any(h.get("tier") == 0 for h in hits) and verdict == "supported":
        return min(confidence, 0.4)
    return max(0.0, min(1.0, confidence))


def run(evidence_path: Path, out_path: Path, model: str, base_url: str, essay_url: str) -> dict:
    payload = read_json(evidence_path)
    client = make_client(base_url)
    claims = []
    by_id = {c["id"]: c for c in (payload.get("claims") or [])}
    for ev in payload.get("evidence") or []:
        claim = dict(by_id.get(ev["id"]) or {"id": ev["id"], "quote": ev.get("quote")})
        label = ev.get("label") or claim.get("label")
        hits = ev.get("hits") or []
        independent = independent_hits(ev.get("quote") or "", hits, essay_url)

        if label != "verifiable":
            claim["verdict"] = "opinion"
            claim["confidence"] = 0.7
            claim["sources"] = []
            claim["why"] = "Classification was not verifiable; no lookup."
            claims.append(claim)
            continue

        if len(independent) < 2:
            claim["verdict"] = "unverifiable"
            claim["confidence"] = 0.35
            claim["sources"] = independent
            claim["why"] = "Fewer than two independent, non-reprint sources."
            claims.append(claim)
            continue

        for _ in range(2):  # one retry: a malformed reply would otherwise become a silent "unverifiable"
            data = extract_json_object(
                chat(
                    client,
                    model,
                    load_prompt("comparator.md"),
                    json.dumps({"claim": claim, "sources": independent[:4]}, ensure_ascii=False),
                    max_tokens=400,
                )
            )
            if data.get("verdict") and data.get("why"):
                break
        verdict = str(data.get("verdict") or "unverifiable").strip().lower()
        if verdict not in {"supported", "contradicted", "misleading", "unverifiable", "opinion"}:
            verdict = "unverifiable"
        try:
            confidence = float(data.get("confidence", 0.4))
        except (TypeError, ValueError):
            confidence = 0.4
        why = str(data.get("why") or "").strip()
        if why.lower() in {"", "one sentence"}:
            why = "Comparator did not explain the verdict from the snippets."
        claim["label"] = label
        claim["verdict"] = verdict
        claim["confidence"] = cap_confidence(verdict, confidence, independent, hits)
        claim["sources"] = [
            {"url": h.get("url"), "title": h.get("title"), "quote": h.get("quote"), "tier": h.get("tier")}
            for h in independent[:4]
        ]
        claim["why"] = why
        claims.append(claim)

    out = {
        "essay_id": payload.get("essay_id"),
        "essay_path": payload.get("essay_path"),
        "essay_url": essay_url,
        "generated_at": now_iso(),
        "agent": f"pipeline/04_compare.py + {model}",
        "claims": claims,
    }
    write_json(out_path, out)
    print(f"compare: {len(claims)} verdicts → {out_path}")
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--evidence", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--model", default="qwen2.5:3b")
    p.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    p.add_argument("--essay-url", default="")
    args = p.parse_args()
    run(args.evidence, args.out, args.model, args.base_url, args.essay_url)


if __name__ == "__main__":
    main()
