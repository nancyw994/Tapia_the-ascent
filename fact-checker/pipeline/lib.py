"""Shared helpers for the fact-checker pipeline."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from openai import OpenAI

try:
    from ddgs import DDGS
except ImportError:  # pragma: no cover
    DDGS = None

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_prompt(name: str) -> str:
    return (CONFIG / "prompts" / name).read_text(encoding="utf-8").strip()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def extract_json_object(text: str) -> dict[str, Any]:
    if not text:
        return {}
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        cleaned = cleaned[start : end + 1]
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def extract_json_array(text: str) -> list[Any]:
    if not text:
        return []
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start >= 0 and end > start:
        cleaned = cleaned[start : end + 1]
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return {}


def chat(client: OpenAI, model: str, system: str, user: str, max_tokens: int = 1800) -> str:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
        max_tokens=max_tokens,
    )
    return (response.choices[0].message.content or "").strip()


def make_client(base_url: str) -> OpenAI:
    return OpenAI(api_key="ollama", base_url=base_url)


def host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def search_web(query: str, limit: int = 4) -> list[dict[str, str]]:
    if DDGS is None:
        return []
    hits: list[dict[str, str]] = []
    try:
        with DDGS(timeout=40) as ddgs:
            for row in ddgs.text(query, region="wt-wt", safesearch="off", max_results=limit):
                url = row.get("href") or row.get("url") or ""
                if not url:
                    continue
                hits.append(
                    {
                        "title": row.get("title") or url,
                        "url": url,
                        "quote": (row.get("body") or "")[:400],
                    }
                )
    except Exception as exc:  # noqa: BLE001
        hits.append({"title": "search-error", "url": "", "quote": str(exc)})
    return hits


def append_source_log(path: Path, claim_id: str, query: str, hits: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(
            "# Source ledger\n\nEvery URL below was fetched during this run.\n"
            "The essay under review is not an independent source.\n\n",
            encoding="utf-8",
        )
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"## {claim_id} — {stamp}", f"Query: `{query}`", ""]
    if not hits:
        lines.append("- no hits")
    for hit in hits:
        url = hit.get("url") or "(none)"
        title = hit.get("title") or ""
        quote = (hit.get("quote") or "").replace("\n", " ")
        tier = hit.get("tier", "")
        lines.append(f"- [{tier}] {url} — {title}" if tier != "" else f"- {url} — {title}")
        if quote:
            lines.append(f"  > {quote[:240]}")
    lines.append("")
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def is_reprint(hit: dict[str, str], quote: str, essay_url: str = "") -> bool:
    url = (hit.get("url") or "").lower()
    title = (hit.get("title") or "").lower()
    snippet = hit.get("quote") or ""
    if essay_url and essay_url.rstrip("/") in url:
        return True
    if "shumer.dev" in url or "somethingbig.ai" in url:
        return True
    if "something big is happening" in title:
        return True
    q = (quote or "").strip()
    if len(q) >= 40 and q[:80] in snippet:
        return True
    return False


def assign_tier(hit: dict[str, str], quote: str, essay_url: str = "") -> int:
    if is_reprint(hit, quote, essay_url):
        return 0
    h = host(hit.get("url") or "")
    if any(h.endswith(x) or h == x.lstrip(".") for x in ("metr.org", "openai.com", "anthropic.com", "who.int")):
        return 1
    if h.endswith(".gov"):
        return 1
    if h in {"arxiv.org"}:
        return 2
    if h in {
        "axios.com",
        "nytimes.com",
        "reuters.com",
        "apnews.com",
        "fortune.com",
        "businessinsider.com",
        "the-decoder.com",
        "cnn.com",
    }:
        return 3
    if h.endswith(".substack.com") or "blog" in h:
        return 4
    return 5


def proceeds_to_search(label: str) -> bool:
    return label in {"verifiable", "verifiable_external"}


def normalize_label(label: str) -> str:
    raw = (label or "").strip().lower()
    if raw in {"verifiable_external", "verifiable"}:
        return "verifiable"
    if raw in {"anecdote", "rhetoric", "forecast", "opinion"}:
        return "opinion"
    if raw == "supported_in_text":
        return "supported_in_text"
    return "opinion"
