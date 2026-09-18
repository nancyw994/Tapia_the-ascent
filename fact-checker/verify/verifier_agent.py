#!/usr/bin/env python3
"""Second-pass agent: challenge verdicts. Does not overwrite human review."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
from lib import chat, extract_json_array, is_reprint, load_prompt, make_client, write_json  # noqa: E402


def heuristic_challenges(verdicts: dict, evidence: dict | None) -> list[dict]:
    hits_by_id = {}
    if evidence:
        for row in evidence.get("evidence") or []:
            hits_by_id[row.get("id")] = row.get("hits") or []
    out = []
    for claim in verdicts.get("claims") or []:
        cid = claim.get("id")
        quote = claim.get("quote") or ""
        sources = claim.get("sources") or []
        why = (claim.get("why") or "").strip().lower()
        reprints = [s for s in sources if is_reprint(s, quote)]
        if claim.get("verdict") == "supported" and reprints:
            out.append(
                {
                    "claim_id": cid,
                    "issue": "circular_source",
                    "agent_said": claim.get("verdict"),
                    "challenge": "Sources reprint the essay; they cannot support the claim.",
                }
            )
        if why in {"one sentence", ""}:
            out.append(
                {
                    "claim_id": cid,
                    "issue": "empty_why",
                    "agent_said": claim.get("verdict"),
                    "challenge": "Reasoning field is empty or a schema leftover.",
                }
            )
        taxonomy = claim.get("taxonomy") or claim.get("label")
        if taxonomy in {"anecdote", "rhetoric"} and claim.get("verdict") == "supported":
            out.append(
                {
                    "claim_id": cid,
                    "issue": "rhetoric_as_verifiable",
                    "agent_said": claim.get("verdict"),
                    "challenge": "Anecdote or rhetoric was stamped supported.",
                }
            )
    return out


def run(verdicts_path: Path, evidence_path: Path | None, out_path: Path, model: str, base_url: str) -> None:
    verdicts = json.loads(verdicts_path.read_text(encoding="utf-8"))
    evidence = json.loads(evidence_path.read_text(encoding="utf-8")) if evidence_path and evidence_path.exists() else None
    challenges = heuristic_challenges(verdicts, evidence)
    try:
        client = make_client(base_url)
        extra = extract_json_array(
            chat(
                client,
                model,
                load_prompt("verifier.md"),
                json.dumps({"claims": verdicts.get("claims", [])[:12]}, ensure_ascii=False)[:8000],
                max_tokens=800,
            )
        )
        for item in extra:
            if isinstance(item, dict) and item.get("claim_id"):
                challenges.append(item)
    except Exception as exc:  # noqa: BLE001
        challenges.append(
            {
                "claim_id": "n/a",
                "issue": "overconfident",
                "agent_said": "n/a",
                "challenge": f"LLM verifier skipped: {exc}",
            }
        )
    write_json(
        out_path,
        {
            "note": "Challenges only. Humans own verify/manual_review.md and error_log.md.",
            "challenges": challenges,
        },
    )
    print(f"verifier: {len(challenges)} challenges → {out_path}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--verdicts", type=Path, required=True)
    p.add_argument("--evidence", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--model", default="qwen2.5:3b")
    p.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    args = p.parse_args()
    run(args.verdicts, args.evidence, args.out, args.model, args.base_url)


if __name__ == "__main__":
    main()
