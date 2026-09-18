"""The four interactive steps behind web/server.py, one function per UI step.

extract_claims → classify_claims → find_sources → decide_verdicts
"""

from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from lib import (
    API_BACKEND,
    append_source_log,
    chat,
    extract_json_array,
    extract_json_object,
    host,
    is_reprint,
    load_prompt,
    make_client,
    search_web,
)
from llm_client import DEFAULT_MODEL  # noqa: E402  (lib.py puts scripts/ on sys.path)

MAX_CLAIMS = 12
SOURCES_PER_CLAIM = 5
WORKERS = 4  # claims searched / judged in parallel
TYPE_COLORS = {"scientific_general": "blue", "event": "orange"}
STANCES = {"supports", "partially_supports", "contradicts", "context", "irrelevant"}
VERDICTS = {"FAKE", "NOT_FAKE", "INSUFFICIENT_EVIDENCE"}

_log_lock = threading.Lock()  # claims are searched in threads; keep ledger entries whole


def _ask(prompt: str, payload, model: str, parse: Callable, is_ok: Callable, tries: int = 2):
    """Call the model, parse the reply, and retry a malformed reply instead of degrading silently."""
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    reply = ""
    for _ in range(tries):
        reply = chat(make_client(API_BACKEND), model, load_prompt(prompt), text)
        data = parse(reply)
        if is_ok(data):
            return data
    raise RuntimeError(f"{prompt}: no usable reply after {tries} tries; last reply: {reply[:200]!r}")


def _clamp(value, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(round(float(value)))))
    except (TypeError, ValueError):
        return 0


# --- Step 2: extract claim sentences -------------------------------------------------

_MD = "[*_`]*"  # emphasis / code markers that may hug a word
_QUOTES = {"'": "['’‘]", "’": "['’‘]", "‘": "['’‘]", '"': '["“”]', "“": '["“”]', "”": '["“”]'}


def _locate(article: str, sentence: str) -> tuple[int, int] | None:
    """Find `sentence` in the article, tolerating whitespace and quote-style differences."""
    sentence = sentence.strip()
    if not sentence:
        return None
    at = article.find(sentence)
    if at >= 0:
        return at, at + len(sentence)
    # Markdown: the model reads "**bold** text" or "> quoted" but copies it without the markup.
    words = [_MD + "".join((_QUOTES.get(ch) or re.escape(ch)) + _MD for ch in word) for word in sentence.split()]
    match = re.search(r"(?:\s|[>*_`])+".join(words), article)
    return (match.start(), match.end()) if match else None


def extract_claims(article: str, model: str = DEFAULT_MODEL) -> list[dict]:
    def build(items: list) -> list[dict]:
        found = []
        for item in items[:MAX_CLAIMS]:
            if isinstance(item, dict) and (span := _locate(article, str(item.get("sentence") or ""))):
                found.append((span, str(item.get("reason") or "").strip()))
        found.sort()
        claims, last_end = [], -1
        for (start, end), reason in found:
            if start < last_end:  # overlaps the previous claim
                continue
            claims.append({"claim_id": f"c{len(claims) + 1:02d}", "sentence": article[start:end], "reason": reason})
            last_end = end
        return claims

    items = _ask("extract_sentences.md", article, model, extract_json_array, lambda d: bool(build(d)))
    return build(items)


# --- Step 4: classify each claim -----------------------------------------------------


def classify_claims(claims: list[dict], model: str = DEFAULT_MODEL) -> list[dict]:
    payload = [{"claim_id": c["claim_id"], "sentence": c["sentence"]} for c in claims]
    expected = {c["claim_id"] for c in claims}

    def parse(reply: str) -> dict[str, dict]:
        rows = extract_json_array(reply)
        return {str(r.get("claim_id")): r for r in rows if isinstance(r, dict) and r.get("type") in TYPE_COLORS}

    rows = _ask("classify_type.md", payload, model, parse, lambda r: expected <= r.keys())
    return [
        {
            "claim_id": c["claim_id"],
            "type": rows[c["claim_id"]]["type"],
            "label_color": TYPE_COLORS[rows[c["claim_id"]]["type"]],
            "reason": str(rows[c["claim_id"]].get("reason") or "").strip(),
        }
        for c in claims
    ]


# --- Step 6: find and rank external sources ------------------------------------------


def _valid_date(value) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", value) else None


def _rank(claim: dict, hits: list[dict], model: str) -> dict[int, dict]:
    payload = {
        "claim": {"claim_id": claim["claim_id"], "sentence": claim["sentence"]},
        "results": [{"index": i, "title": h["title"], "url": h["url"], "snippet": h["quote"]} for i, h in enumerate(hits)],
    }

    def parse(reply: str) -> dict[int, dict]:
        rows = extract_json_array(reply)
        return {int(r["index"]): r for r in rows if isinstance(r, dict) and str(r.get("index", "")).isdigit()}

    try:
        return _ask("rank_sources.md", payload, model, parse, bool)
    except RuntimeError:
        return {}  # unscored sources are still shown, flagged as such


def _sources_for(claim: dict, queries: list[str], log_path: Path, model: str) -> list[dict]:
    seen: set[str] = set()
    hits: list[dict] = []
    for query in queries:
        batch = search_web(query, limit=5)
        with _log_lock:
            append_source_log(log_path, claim["claim_id"], query, batch)
        for hit in batch:
            url = hit.get("url") or ""
            # Drop empty results and pages that just quote the claim back (not independent).
            if url and url not in seen and not is_reprint(hit, claim["sentence"]):
                seen.add(url)
                hits.append(hit)
    hits = hits[:8]
    if not hits:
        return []
    rows = _rank(claim, hits, model)
    sources = []
    for i, hit in enumerate(hits):
        row = rows.get(i, {})
        stance = row.get("stance") if row.get("stance") in STANCES else "context"
        sources.append(
            {
                "title": hit["title"],
                "publisher": str(row.get("publisher") or "").strip() or host(hit["url"]),
                "date": _valid_date(row.get("date")),
                "url": hit["url"],
                "relevance_score": _clamp(row.get("relevance_score"), -100, 100),
                "stance": stance,
                "reason": str(row.get("reason") or "").strip() or "Not scored: the model's rating was unusable.",
                "snippet": hit["quote"],
            }
        )
    sources.sort(key=lambda s: s["relevance_score"], reverse=True)
    sources = sources[:SOURCES_PER_CLAIM]
    for n, source in enumerate(sources, start=1):
        source["source_id"] = f"{claim['claim_id']}_s{n}"
    return sources


def find_sources(claims: list[dict], log_path: Path, model: str = DEFAULT_MODEL) -> list[dict]:
    plan = _ask(
        "plan_queries.md",
        [{"claim_id": c["claim_id"], "sentence": c["sentence"]} for c in claims],
        model,
        extract_json_object,
        bool,
    )

    def work(claim: dict) -> dict:
        planned = plan.get(claim["claim_id"]) or []
        queries = [str(q).strip() for q in planned if str(q).strip()][:2] or [claim["sentence"][:120]]
        try:
            return {"claim_id": claim["claim_id"], "sources": _sources_for(claim, queries, log_path, model)}
        except Exception as exc:  # noqa: BLE001  one claim failing must not sink the others
            return {"claim_id": claim["claim_id"], "sources": [], "error": f"{type(exc).__name__}: {exc}"}

    with ThreadPoolExecutor(WORKERS) as pool:
        return list(pool.map(work, claims))


# --- Step 8: final verdict -----------------------------------------------------------


def _decide(claim: dict, sources: list[dict], model: str) -> dict:
    def insufficient(reason: str) -> dict:
        return {
            "claim_id": claim["claim_id"],
            "verdict": "INSUFFICIENT_EVIDENCE",
            "confidence": 0,
            "reason": reason,
            "supporting_source_ids": [],
        }

    if not sources:
        return insufficient("No independent sources were retrieved for this claim.")
    payload = {
        "claim": {"claim_id": claim["claim_id"], "sentence": claim["sentence"]},
        "sources": [
            {k: s[k] for k in ("source_id", "publisher", "date", "stance", "relevance_score", "snippet")}
            for s in sources
        ],
    }

    def parse(reply: str) -> dict:
        data = extract_json_object(reply)
        data["verdict"] = str(data.get("verdict") or "").strip().upper().replace(" ", "_")
        return data

    try:
        data = _ask("final_verdict.md", payload, model, parse, lambda d: d["verdict"] in VERDICTS and d.get("reason"))
    except RuntimeError:
        return insufficient("The model's verdict could not be read; treat this claim as unchecked.")
    valid_ids = {s["source_id"] for s in sources}
    cited = [i for i in data.get("supporting_source_ids") or [] if i in valid_ids]
    verdict, reason = data["verdict"], str(data["reason"]).strip()
    if verdict != "INSUFFICIENT_EVIDENCE" and not cited:
        # A FAKE / NOT_FAKE call that cites no retrieved source is not grounded in evidence.
        verdict, reason = "INSUFFICIENT_EVIDENCE", f"{reason} (Downgraded: no retrieved source was cited.)"
    return {
        "claim_id": claim["claim_id"],
        "verdict": verdict,
        "confidence": _clamp(data.get("confidence"), 0, 100),
        "reason": reason,
        "supporting_source_ids": cited,
    }


def decide_verdicts(claims: list[dict], evidence: list[dict], model: str = DEFAULT_MODEL) -> list[dict]:
    sources_by_claim = {e["claim_id"]: e["sources"] for e in evidence}

    def work(claim: dict) -> dict:
        try:
            return _decide(claim, sources_by_claim.get(claim["claim_id"], []), model)
        except Exception as exc:  # noqa: BLE001
            return {
                "claim_id": claim["claim_id"],
                "verdict": "INSUFFICIENT_EVIDENCE",
                "confidence": 0,
                "reason": f"Agent error, not a judgment: {type(exc).__name__}: {exc}",
                "supporting_source_ids": [],
            }

    with ThreadPoolExecutor(WORKERS) as pool:
        return list(pool.map(work, claims))
