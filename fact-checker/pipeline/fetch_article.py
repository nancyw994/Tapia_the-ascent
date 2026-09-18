"""Fetch an article URL into markdown/text. Direct GET, then r.jina.ai via curl."""

from __future__ import annotations

import html as html_lib
import re
import subprocess
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

MIN_CHARS = 200
TIMEOUT = 40
USER_AGENT = "Mozilla/5.0 (compatible; TAPIAClaimChecker/1.0; +https://localhost)"
JINA_PREFIX = "https://r.jina.ai/"
DENIED = (
    "access denied",
    "just a moment",
    "enable javascript",
    "you don't have permission to access",
)


def normalize_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        raise ValueError("Paste an http(s) link.")
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    parts = urlsplit(raw)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _denied(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in DENIED)


def html_to_text(raw: str) -> str:
    cleaned = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    cleaned = re.sub(r"(?is)<br\s*/?>", "\n", cleaned)
    cleaned = re.sub(r"(?is)</p>", "\n\n", cleaned)
    cleaned = re.sub(r"(?is)</h[1-6]>", "\n\n", cleaned)
    cleaned = re.sub(r"(?is)<[^>]+>", " ", cleaned)
    cleaned = html_lib.unescape(cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def strip_jina(text: str) -> str:
    if "Markdown Content:" in text:
        return text.split("Markdown Content:", 1)[1].strip()
    return text.strip()


def _curl(url: str) -> tuple[int, str]:
    result = subprocess.run(
        ["curl", "-sS", "-L", "-A", USER_AGENT, "--max-time", str(TIMEOUT), "-w", "\n__HTTPSTATUS__%{http_code}", url],
        capture_output=True,
        text=True,
        check=False,
    )
    raw = result.stdout or ""
    status = 0
    if "__HTTPSTATUS__" in raw:
        body, _, code = raw.rpartition("__HTTPSTATUS__")
        raw = body
        try:
            status = int(code.strip() or "0")
        except ValueError:
            status = 0
    elif result.returncode != 0:
        status = 599
    else:
        status = 200
    return status, raw


def fetch_url_markdown(url: str) -> tuple[str, str]:
    """Return (markdown_body, canonical_url)."""
    canonical = normalize_url(url)
    tried: list[str] = []

    def accept(label: str, status: int, raw: str, *, jina: bool) -> str | None:
        tried.append(f"{label}:{status}")
        if status >= 400 or not (raw or "").strip():
            return None
        text = strip_jina(raw) if jina or "Markdown Content:" in raw else html_to_text(raw)
        if raw.lstrip().lower().startswith("<!") and not jina:
            text = html_to_text(raw)
        if _denied(text) or len(text) < MIN_CHARS:
            return None
        return text

    try:
        response = requests.get(
            canonical,
            timeout=TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain,*/*;q=0.8"},
            allow_redirects=True,
        )
        ctype = (response.headers.get("content-type") or "").lower()
        raw = response.text or ""
        text = accept("direct", response.status_code, raw, jina="plain" in ctype or "markdown" in ctype)
        if text:
            return text, canonical
    except requests.RequestException:
        tried.append("direct:error")

    jina_url = JINA_PREFIX + canonical
    for attempt in range(1, 5):
        status, raw = _curl(jina_url)
        text = accept(f"jina-{attempt}", status, raw, jina=True)
        if text:
            return text, canonical
        if attempt < 4:
            time.sleep(1.2 * attempt)

    raise ValueError(
        "Could not extract article text from that link ("
        + ", ".join(tried)
        + "). Upload a .md/.txt file instead."
    )
