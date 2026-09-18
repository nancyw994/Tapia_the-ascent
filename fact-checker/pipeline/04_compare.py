#!/usr/bin/env python3
"""Stage 4: evidence.json → verdicts.json"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import chat, extract_json_object, load_prompt, make_client, now_iso, read_json, write_json
from source_verification import SourcePolicy


def independent_hits(hits: list[dict]) -> list[dict]:
    """Hits that source_verification.py fetched, tier-classified, and found
    claim-relevant text in (`eligible`), deduped to one per publisher domain.

    `eligible` already encodes: not a reprint of the essay, from a tier whose
    policy allows it to support a verdict, and containing text relevant to
    the claim in the fetched page — not just a search-result snippet.
    """
    kept = []
    seen_publishers: set[str] = set()
    for hit in hits:
        if not hit.get("eligible"):
            continue
        publisher = hit.get("publisher") or hit.get("host") or hit.get("final_url") or hit.get("url")
        if not publisher or publisher in seen_publishers:
            continue
        seen_publishers.add(publisher)
        kept.append(hit)
    return kept


def sufficient_evidence(independent: list[dict], policy: SourcePolicy) -> bool:
    """≥min_sources independent hits, OR exactly one hit trusted enough to stand alone."""
    if len(independent) >= policy.min_sources:
        return True
    single_tier = policy.independence.get("single_source_min_tier")
    if single_tier is not None and len(independent) == 1:
        tier = independent[0].get("tier")
        return tier is not None and tier <= single_tier
    return False


def cap_confidence(verdict: str, confidence: float, independent: list, hits: list, policy: SourcePolicy) -> float:
    if verdict == "supported" and len(independent) < policy.min_sources:
        cap = policy.independence.get("single_source_confidence_cap", 0.0)
        return min(confidence, cap)
    if any(h.get("reprint") for h in hits) and verdict == "supported":
        return min(confidence, 0.4)
    return max(0.0, min(1.0, confidence))


def shape_sources(hits: list[dict]) -> list[dict]:
    """Trim a verified hit down to what verdicts.json / the annotator need."""
    return [
        {
            "url": h.get("final_url") or h.get("url"),
            "title": h.get("title"),
            "quote": h.get("evidence_excerpt") or h.get("quote"),
            "tier": h.get("tier"),
            "tier_name": h.get("tier_name"),
        }
        for h in hits
    ]


def run(evidence_path: Path, out_path: Path, model: str, base_url: str, essay_url: str) -> dict:
    payload = read_json(evidence_path)
    client = make_client(base_url)
    policy = SourcePolicy()
    claims = []
    by_id = {c["id"]: c for c in (payload.get("claims") or [])}
    for ev in payload.get("evidence") or []:
        claim = dict(by_id.get(ev["id"]) or {"id": ev["id"], "quote": ev.get("quote")})
        label = ev.get("label") or claim.get("label")
        hits = ev.get("hits") or []
        independent = independent_hits(hits)

        if label != "verifiable":
            claim["verdict"] = "opinion"
            claim["confidence"] = 0.7
            claim["sources"] = []
            claim["why"] = "Classification was not verifiable; no lookup."
            claims.append(claim)
            continue

        if not sufficient_evidence(independent, policy):
            claim["verdict"] = "unverifiable"
            claim["confidence"] = 0.35
            claim["sources"] = shape_sources(independent)
            claim["why"] = (
                "Fewer than two independent, non-reprint sources, and no single source was"
                " trusted enough to stand alone."
                if independent
                else "No eligible independent sources were retrieved."
            )
            claims.append(claim)
            continue

        # Ground the comparator in text actually found on the fetched page (evidence_excerpt),
        # not the raw search-engine snippet, which can misdescribe or predate a redirected page.
        for _ in range(2):  # one retry: a malformed reply would otherwise become a silent "unverifiable"
            data = extract_json_object(
                chat(
                    client,
                    model,
                    load_prompt("comparator.md"),
                    json.dumps({"claim": claim, "sources": shape_sources(independent[:4])}, ensure_ascii=False),
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
        claim["confidence"] = cap_confidence(verdict, confidence, independent, hits, policy)
        claim["sources"] = shape_sources(independent[:4])
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
