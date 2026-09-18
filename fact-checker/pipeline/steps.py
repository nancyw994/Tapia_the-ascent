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
MAX_CLAIMS_PER_WINDOW = 8
WINDOW_CHARS = 4000
WINDOW_OVERLAP = 500
SOURCES_PER_CLAIM = 5
WORKERS = 4  # claims searched / judged in parallel
VERIFY_WORKERS = 6  # hits verified (fetched) per claim in parallel; network-bound
TYPE_COLORS = {"scientific_general": "blue", "event": "orange"}
STANCES = {"supports", "partially_supports", "contradicts", "context", "irrelevant"}
VERDICTS = {"supported", "contradicted", "misleading", "unverifiable", "opinion"}
_VERDICT_ALIASES = {
    "fake": "contradicted",
    "false": "contradicted",
    "contradicted": "contradicted",
    "not_fake": "supported",
    "true": "supported",
    "supported": "supported",
    "misleading": "misleading",
    "insufficient_evidence": "unverifiable",
    "insufficient": "unverifiable",
    "unverifiable": "unverifiable",
    "opinion": "opinion",
    "anecdote": "opinion",
}
NEEDS_REVIEW = {"unverifiable", "opinion"}


def normalize_verdict(value: str) -> str:
    raw = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    return _VERDICT_ALIASES.get(raw, "")


def article_coverage(claims: list[dict], evidence: list[dict], verdicts: list[dict]) -> dict:
    """Run-level counts. This is coverage, not an article-level fake-news score."""
    checked = 0
    independent = 0
    reprints = 0
    for row in evidence or []:
        reprints += int(row.get("reprints_dropped") or 0)
        sources = row.get("sources") or []
        if sources:
            checked += 1
        hosts = {host(src.get("url") or "") for src in sources if src.get("url")}
        hosts.discard("")
        if len(hosts) >= 2:
            independent += 1
    pending = 0
    reviewed = 0
    ev_by = {row.get("claim_id"): row for row in evidence or []}
    for row in verdicts or []:
        if row.get("human_override"):
            reviewed += 1
            continue
        sources = (ev_by.get(row.get("claim_id")) or {}).get("sources") or []
        low_conf = False
        try:
            low_conf = int(row.get("confidence") or 0) < 50
        except (TypeError, ValueError):
            low_conf = True
        if row.get("verdict") in NEEDS_REVIEW or low_conf or len(sources) < 2:
            pending += 1
    return {
        "claims_found": len(claims or []),
        "claims_checked": checked,
        "reprints_dropped": reprints,
        "independent_enough": independent,
        "pending_review": pending,
        "human_reviewed": reviewed,
    }

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


def article_windows(article: str, size: int = WINDOW_CHARS, overlap: int = WINDOW_OVERLAP) -> list[str]:
    """Split a long article so later paragraphs are not dropped by a single-pass cap."""
    text = article or ""
    n = len(text)
    if n <= size:
        return [text] if text.strip() else []
    windows: list[str] = []
    start = 0
    while start < n:
        end = min(n, start + size)
        if end < n:
            cut = text.rfind("\n\n", start + size // 2, end)
            if cut < start + 800:
                cut = text.rfind(". ", start + size // 2, end)
                end = cut + 1 if cut >= start + 800 else end
            else:
                end = cut
        if text[start:end].strip():
            windows.append(text[start:end])
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return windows


def _spans_overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return not (left[1] <= right[0] or right[1] <= left[0])


def _renumber_claims(article: str, claims: list[dict]) -> list[dict]:
    ordered = []
    for row in claims:
        span = _locate(article, row.get("sentence") or "")
        if not span:
            continue
        ordered.append((span, row))
    ordered.sort(key=lambda item: item[0][0])
    out: list[dict] = []
    last_end = -1
    for (start, end), row in ordered:
        if start < last_end:
            continue
        item = dict(row)
        item["claim_id"] = f"c{len(out) + 1:02d}"
        item["sentence"] = article[start:end]
        out.append(item)
        last_end = end
        if len(out) >= MAX_CLAIMS:
            break
    return out


def extract_claims(article: str, model: str = DEFAULT_MODEL, emit: Emit = None) -> list[dict]:
    found: list[tuple[tuple[int, int], str]] = []
    windows = article_windows(article)
    for i, window in enumerate(windows, start=1):
        if len(found) >= MAX_CLAIMS:
            break
        try:
            items = _ask(
                "extract_sentences.md",
                window,
                model,
                extract_json_array,
                lambda d: isinstance(d, list),
                emit=emit,
                task=f"extract-{i}",
                label=f"Reading section {i} of {len(windows)}",
            )
        except RuntimeError:
            continue
        room = MAX_CLAIMS - len(found)
        for item in (items or [])[: min(MAX_CLAIMS_PER_WINDOW, room)]:
            if not isinstance(item, dict):
                continue
            span = _locate(article, str(item.get("sentence") or ""))
            if not span:
                continue
            if any(_spans_overlap(span, existing) for existing, _ in found):
                continue
            found.append((span, str(item.get("reason") or "").strip()))
    found.sort(key=lambda item: item[0][0])
    claims: list[dict] = []
    last_end = -1
    for (start, end), reason in found:
        if start < last_end:
            continue
        claims.append({"claim_id": f"c{len(claims) + 1:02d}", "sentence": article[start:end], "reason": reason})
        last_end = end
        if len(claims) >= MAX_CLAIMS:
            break
    if not claims:
        raise RuntimeError("no checkable claims were found in any section of the article")
    return claims


def add_claim(article: str, claims: list[dict], sentence: str, reason: str = "") -> list[dict]:
    span = _locate(article, sentence)
    if not span:
        raise ValueError("that sentence was not found in the article; paste it verbatim")
    start, end = span
    for row in claims:
        other = _locate(article, row.get("sentence") or "")
        if other and _spans_overlap((start, end), other):
            raise ValueError("that sentence already overlaps an extracted claim")
    if len(claims) >= MAX_CLAIMS:
        raise ValueError(f"this run already has {MAX_CLAIMS} claims")
    extra = {
        "claim_id": "tmp",
        "sentence": article[start:end],
        "reason": (reason or "").strip() or "Added by a human reviewer.",
        "human_added": True,
    }
    return _renumber_claims(article, list(claims) + [extra])


def drop_claim(article: str, claims: list[dict], claim_id: str) -> list[dict]:
    kept = [row for row in claims if row.get("claim_id") != claim_id]
    if len(kept) == len(claims):
        raise ValueError("unknown claim")
    if not kept:
        raise ValueError("keep at least one claim")
    return _renumber_claims(article, kept)





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
) -> tuple[list[dict], int]:
    cid = claim["claim_id"]
    note = (lambda text: emit({"task": f"search-{cid}", "label": f"{cid}: searching the web", "type": "note", "text": text})) if emit else _silent
    seen: set[str] = set()
    hits: list[dict] = []
    dropped = 0
    for query in queries:
        note(f"searching: {query}")
        batch = search_web(query, limit=5)
        with _log_lock:
            append_source_log(log_path, claim["claim_id"], query, batch)
        for hit in batch:
            url = hit.get("url") or ""
            if not url or url in seen:
                continue
            if is_reprint(hit, claim["sentence"], essay_url):
                dropped += 1
                continue
            seen.add(url)
            hits.append(hit)
    hits = hits[:8]
    note(f"verifying {len(hits)} result{'' if len(hits) == 1 else 's'} (fetching, checking tier and relevance)")
    if not hits:
        return [], dropped

    def verify_one(hit: dict) -> dict:
        verdict = _verifier.verify(hit, claim["sentence"], essay_text, essay_url)
        return {**hit, **verdict}

    with ThreadPoolExecutor(VERIFY_WORKERS) as pool:
        hits = list(pool.map(verify_one, hits))
    dropped += sum(1 for h in hits if h.get("reprint"))
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
    return sources, dropped


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
            sources, dropped = _sources_for(
                claim, queries, log_path, model, emit=emit, essay_text=essay_text, essay_url=essay_url
            )
            return {"claim_id": claim["claim_id"], "sources": sources, "reprints_dropped": dropped}
        except Exception as exc:  # noqa: BLE001  one claim failing must not sink the others
            return {"claim_id": claim["claim_id"], "sources": [], "reprints_dropped": 0, "error": f"{type(exc).__name__}: {exc}"}

    with ThreadPoolExecutor(WORKERS) as pool:
        return list(pool.map(work, claims))


# --- Step 8: final verdict -----------------------------------------------------------


def _decide(claim: dict, sources: list[dict], model: str, emit: Emit = None, reprints_dropped: int = 0) -> dict:
    def insufficient(reason: str) -> dict:
        return {
            "claim_id": claim["claim_id"],
            "verdict": "unverifiable",
            "confidence": 0,
            "reason": reason,
            "supporting_source_ids": [],
        }

    if not sources:
        reason = "No independent sources were retrieved for this claim."
        if reprints_dropped:
            reason += f" {reprints_dropped} reprint(s) of this essay were dropped."
        return insufficient(reason)

    # Gate on verified, tier-eligible evidence before asking the model.
    eligible = independent_hits(sources)
    if not sufficient_evidence(eligible, _policy):
        extra = f" {reprints_dropped} reprint(s) of this essay were dropped." if reprints_dropped else ""
        return insufficient(
            (
                "Only one independently-verified source was found and it wasn't trusted enough to"
                " stand alone (needs an official/peer-reviewed source, or a second independent one)."
                if eligible
                else "No independently-verified source (fetched, correctly attributed, relevant) was found for this claim."
            )
            + extra
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
        data["verdict"] = normalize_verdict(data.get("verdict"))
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
    confidence = _clamp(data.get("confidence"), 0, 100)
    if verdict not in {"unverifiable", "opinion"} and not cited:
        verdict, reason = "unverifiable", f"{reason} (Downgraded: no retrieved source was cited.)"
    if verdict not in {"unverifiable", "opinion"}:
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
    sources_by_claim = {e["claim_id"]: e for e in evidence}

    def work(claim: dict) -> dict:
        row = sources_by_claim.get(claim["claim_id"]) or {}
        try:
            return _decide(
                claim,
                row.get("sources") or [],
                model,
                emit=emit,
                reprints_dropped=int(row.get("reprints_dropped") or 0),
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "claim_id": claim["claim_id"],
                "verdict": "unverifiable",
                "confidence": 0,
                "reason": f"Agent error, not a judgment: {type(exc).__name__}: {exc}",
                "supporting_source_ids": [],
            }

    with ThreadPoolExecutor(WORKERS) as pool:
        return list(pool.map(work, claims))

