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
from source_verification import (
    SourcePolicy,
    SourceVerifier,
    cap_single_source_confidence,
    independent_hits,
    sufficient_evidence,
)

MAX_CLAIMS = 12
SOURCES_PER_CLAIM = 5
WORKERS = 4  # claims searched / judged in parallel
VERIFY_WORKERS = 6  # hits verified (fetched) per claim in parallel; network-bound
TYPE_COLORS = {"scientific_general": "blue", "event": "orange"}
STANCES = {"supports", "partially_supports", "contradicts", "context", "irrelevant"}
VERDICTS = {"FAKE", "NOT_FAKE", "INSUFFICIENT_EVIDENCE"}

_log_lock = threading.Lock()  # claims are searched in threads; keep ledger entries whole
_policy = SourcePolicy()
_verifier = SourceVerifier(_policy)  # shared requests.Session; safe across the thread pools below

# Called with one event dict per start / token / note / end, for the live view in the browser.
Emit = Callable[[dict], None] | None


def _silent(kind: str, **fields) -> None:
    pass


def _ask(
    prompt: str,
    payload,
    model: str,
    parse: Callable,
    is_ok: Callable,
    tries: int = 2,
    emit: Emit = None,
    task: str = "",
    label: str = "",
):
    """Call the model, parse the reply, and retry a malformed reply instead of degrading silently.

    With `emit`, the reply is streamed so the page can show it arriving token by token.
    """
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    say = (lambda kind, **f: emit({"task": task, "label": label, "type": kind, **f})) if emit else _silent
    on_token = (lambda chunk, thinking: say("token", text=chunk, reasoning=thinking)) if emit else None
    say("start")
    reply = ""
    for attempt in range(tries):
        if attempt:
            say("note", text="That reply could not be parsed — asking again.")
        reply = chat(make_client(API_BACKEND), model, load_prompt(prompt), text, on_token=on_token)
        data = parse(reply)
        if is_ok(data):
            say("end")
            return data
    say("end", failed=True)
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


def extract_claims(article: str, model: str = DEFAULT_MODEL, emit: Emit = None) -> list[dict]:
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

    items = _ask(
        "extract_sentences.md",
        article,
        model,
        extract_json_array,
        lambda d: bool(build(d)),
        emit=emit,
        task="extract",
        label="Reading the article and picking out testable claims",
    )
    return build(items)


# --- Step 4: classify each claim -----------------------------------------------------


def classify_claims(claims: list[dict], model: str = DEFAULT_MODEL, emit: Emit = None) -> list[dict]:
    payload = [{"claim_id": c["claim_id"], "sentence": c["sentence"]} for c in claims]
    expected = {c["claim_id"] for c in claims}

    def parse(reply: str) -> dict[str, dict]:
        rows = extract_json_array(reply)
        return {str(r.get("claim_id")): r for r in rows if isinstance(r, dict) and r.get("type") in TYPE_COLORS}

    rows = _ask(
        "classify_type.md",
        payload,
        model,
        parse,
        lambda r: expected <= r.keys(),
        emit=emit,
        task="classify",
        label=f"Sorting {len(claims)} claims into scientific / general vs event",
    )
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


def _rank(claim: dict, hits: list[dict], model: str, emit: Emit = None) -> dict[int, dict]:
    payload = {
        "claim": {"claim_id": claim["claim_id"], "sentence": claim["sentence"]},
        "results": [{"index": i, "title": h["title"], "url": h["url"], "snippet": h["quote"]} for i, h in enumerate(hits)],
    }

    def parse(reply: str) -> dict[int, dict]:
        rows = extract_json_array(reply)
        return {int(r["index"]): r for r in rows if isinstance(r, dict) and str(r.get("index", "")).isdigit()}

    try:
        return _ask(
            "rank_sources.md",
            payload,
            model,
            parse,
            bool,
            emit=emit,
            task=f"rank-{claim['claim_id']}",
            label=f"{claim['claim_id']}: scoring {len(hits)} search results",
        )
    except RuntimeError:
        return {}  # unscored sources are still shown, flagged as such


def _sources_for(
    claim: dict, queries: list[str], log_path: Path, model: str, emit: Emit = None, essay_text: str = "", essay_url: str = ""
) -> list[dict]:
    cid = claim["claim_id"]
    note = (lambda text: emit({"task": f"search-{cid}", "label": f"{cid}: searching the web", "type": "note", "text": text})) if emit else _silent
    seen: set[str] = set()
    hits: list[dict] = []
    for query in queries:
        note(f"searching: {query}")
        batch = search_web(query, limit=5)
        with _log_lock:
            append_source_log(log_path, claim["claim_id"], query, batch)
        for hit in batch:
            url = hit.get("url") or ""
            # Drop empty results and pages that just quote the claim back (not independent).
            if url and url not in seen and not is_reprint(hit, claim["sentence"], essay_url):
                seen.add(url)
                hits.append(hit)
    hits = hits[:8]
    note(f"verifying {len(hits)} result{'' if len(hits) == 1 else 's'} (fetching, checking tier and relevance)")
    if not hits:
        return []

    def verify_one(hit: dict) -> dict:
        verdict = _verifier.verify(hit, claim["sentence"], essay_text, essay_url)
        return {**hit, **verdict}

    with ThreadPoolExecutor(VERIFY_WORKERS) as pool:
        hits = list(pool.map(verify_one, hits))
    note(f"{sum(h.get('eligible', False) for h in hits)} of {len(hits)} are independently verified")

    rows = _rank(claim, hits, model, emit)
    sources = []
    for i, hit in enumerate(hits):
        row = rows.get(i, {})
        stance = row.get("stance") if row.get("stance") in STANCES else "context"
        sources.append(
            {
                "title": hit.get("title") or "",
                "publisher": hit.get("publisher") or str(row.get("publisher") or "").strip() or host(hit["url"]),
                "date": _valid_date(row.get("date")) or (hit.get("published_at") or None),
                "url": hit.get("final_url") or hit["url"],
                "relevance_score": _clamp(row.get("relevance_score"), -100, 100),
                "stance": stance,
                "reason": str(row.get("reason") or "").strip() or "Not scored: the model's rating was unusable.",
                "snippet": hit.get("evidence_excerpt") or hit["quote"],
                "tier": hit.get("tier"),
                "tier_name": hit.get("tier_name"),
                "eligible": bool(hit.get("eligible")),
            }
        )
    sources.sort(key=lambda s: s["relevance_score"], reverse=True)
    sources = sources[:SOURCES_PER_CLAIM]
    for n, source in enumerate(sources, start=1):
        source["source_id"] = f"{claim['claim_id']}_s{n}"
    return sources


def find_sources(
    claims: list[dict],
    log_path: Path,
    model: str = DEFAULT_MODEL,
    emit: Emit = None,
    essay_text: str = "",
    essay_url: str = "",
) -> list[dict]:
    plan = _ask(
        "plan_queries.md",
        [{"claim_id": c["claim_id"], "sentence": c["sentence"]} for c in claims],
        model,
        extract_json_object,
        bool,
        emit=emit,
        task="queries",
        label="Writing search queries for every claim",
    )

    def work(claim: dict) -> dict:
        planned = plan.get(claim["claim_id"]) or []
        queries = [str(q).strip() for q in planned if str(q).strip()][:2] or [claim["sentence"][:120]]
        try:
            sources = _sources_for(claim, queries, log_path, model, emit, essay_text, essay_url)
            return {"claim_id": claim["claim_id"], "sources": sources}
        except Exception as exc:  # noqa: BLE001  one claim failing must not sink the others
            return {"claim_id": claim["claim_id"], "sources": [], "error": f"{type(exc).__name__}: {exc}"}

    with ThreadPoolExecutor(WORKERS) as pool:
        return list(pool.map(work, claims))


# --- Step 8: final verdict -----------------------------------------------------------


def _decide(claim: dict, sources: list[dict], model: str, emit: Emit = None) -> dict:
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

    # Gate on verified, tier-eligible evidence *before* asking the model, and only ever
    # show it eligible sources — an unverified/untrusted hit (wrong tier, unreachable page,
    # or no relevant text found) can never be cited into a FAKE/NOT_FAKE verdict this way.
    eligible = independent_hits(sources)
    if not sufficient_evidence(eligible, _policy):
        return insufficient(
            "Only one independently-verified source was found and it wasn't trusted enough to"
            " stand alone (needs an official/peer-reviewed source, or a second independent one)."
            if eligible
            else "No independently-verified source (fetched, correctly attributed, relevant) was found for this claim."
        )

    payload = {
        "claim": {"claim_id": claim["claim_id"], "sentence": claim["sentence"]},
        "sources": [
            {k: s[k] for k in ("source_id", "publisher", "date", "stance", "relevance_score", "snippet")}
            for s in eligible
        ],
    }

    def parse(reply: str) -> dict:
        data = extract_json_object(reply)
        data["verdict"] = str(data.get("verdict") or "").strip().upper().replace(" ", "_")
        return data

    try:
        data = _ask(
            "final_verdict.md",
            payload,
            model,
            parse,
            lambda d: d["verdict"] in VERDICTS and d.get("reason"),
            emit=emit,
            task=f"verdict-{claim['claim_id']}",
            label=f"{claim['claim_id']}: weighing {len(eligible)} verified sources",
        )
    except RuntimeError:
        return insufficient("The model's verdict could not be read; treat this claim as unchecked.")
    valid_ids = {s["source_id"] for s in eligible}
    cited = [i for i in data.get("supporting_source_ids") or [] if i in valid_ids]
    verdict, reason = data["verdict"], str(data["reason"]).strip()
    if verdict != "INSUFFICIENT_EVIDENCE" and not cited:
        # A FAKE / NOT_FAKE call that cites no retrieved source is not grounded in evidence.
        verdict, reason = "INSUFFICIENT_EVIDENCE", f"{reason} (Downgraded: no retrieved source was cited.)"
    confidence = _clamp(data.get("confidence"), 0, 100)
    if verdict != "INSUFFICIENT_EVIDENCE":
        # A confident FAKE/NOT_FAKE call resting on a single source, however trusted, should
        # not read as more certain than one that's actually independently corroborated.
        confidence = int(cap_single_source_confidence(confidence, eligible, _policy, scale=100))
    return {
        "claim_id": claim["claim_id"],
        "verdict": verdict,
        "confidence": confidence,
        "reason": reason,
        "supporting_source_ids": cited,
    }


def decide_verdicts(
    claims: list[dict], evidence: list[dict], model: str = DEFAULT_MODEL, emit: Emit = None
) -> list[dict]:
    sources_by_claim = {e["claim_id"]: e["sources"] for e in evidence}

    def work(claim: dict) -> dict:
        try:
            return _decide(claim, sources_by_claim.get(claim["claim_id"], []), model, emit)
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
