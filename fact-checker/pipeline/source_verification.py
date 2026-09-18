"""Safely retrieve, classify, and ground evidence from web sources.

Search results are discovery hints, never evidence.  A result becomes usable only
after this module has fetched the final document, extracted readable text, checked
that it is not a reprint of the submitted essay, and found claim-relevant text.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
import tldextract
import yaml
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH = ROOT / "config" / "source_tiers.yaml"
MAX_DOCUMENT_BYTES = 2_000_000
MAX_REDIRECTS = 4
TRACKING_PARAMETERS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_", "source"}
TEXT_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
USER_AGENT = "TAPIA-Claim-Checker/0.3 (+https://github.com/nancyw994/Tapia_the-ascent)"
DOMAIN_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=())


class UnsafeSourceUrl(ValueError):
    """A URL is not safe for a server-side verifier to request."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonicalize_url(value: str) -> str:
    """Return a stable public HTTP(S) URL, dropping common tracking parameters."""
    parsed = urlsplit((value or "").strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise UnsafeSourceUrl("only absolute http(s) URLs are allowed")
    if parsed.username or parsed.password:
        raise UnsafeSourceUrl("URLs with credentials are not allowed")
    host = parsed.hostname.lower().rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeSourceUrl("invalid host name") from exc
    port = parsed.port
    netloc = host if port is None or (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443) else f"{host}:{port}"
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMETERS and not key.lower().startswith("utm_")
    ]
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", urlencode(sorted(query)), ""))


def public_hostname(url: str) -> bool:
    """Reject localhost and names resolving to private/reserved IP ranges (SSRF guard)."""
    host = urlsplit(url).hostname
    if not host or host.lower() in {"localhost", "localhost.localdomain"}:
        return False
    try:
        addresses = {row[4][0] for row in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
    except OSError:
        return False
    if not addresses:
        return False
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return False
    return True


def registered_host(url: str) -> str:
    """Return the registrable publisher domain without making a network request."""
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    extracted = DOMAIN_EXTRACTOR(host)
    return extracted.top_domain_under_public_suffix or host


def _normalise_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value or "")).strip().lower()


def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9][a-z0-9'-]{1,}", _normalise_text(value)) if len(token) > 2}


def _excerpt(text: str, start: int, end: int, padding: int = 280) -> str:
    left = max(0, start - padding)
    right = min(len(text), end + padding)
    return re.sub(r"\s+", " ", text[left:right]).strip()


def find_claim_evidence(claim: str, document: str) -> dict[str, Any]:
    """Find a quoted or high-overlap passage in retrieved source text.

    This deliberately reports relevance, not truth. The comparator remains
    responsible for deciding whether a relevant passage supports or contradicts
    a claim.
    """
    clean_claim = _normalise_text(claim)
    clean_document = _normalise_text(document)
    if len(clean_claim) < 8 or len(clean_document) < 8:
        return {"relevance": "none", "score": 0.0, "excerpt": ""}
    direct_at = clean_document.find(clean_claim)
    if direct_at >= 0:
        return {"relevance": "direct", "score": 1.0, "excerpt": _excerpt(document, direct_at, direct_at + len(clean_claim))}

    claim_tokens = _tokens(claim)
    if len(claim_tokens) < 2:
        return {"relevance": "none", "score": 0.0, "excerpt": ""}
    candidates = re.split(r"(?<=[.!?])\s+|\n{2,}", document)
    best_score, best = 0.0, ""
    for index, sentence in enumerate(candidates):
        window = " ".join(candidates[index : index + 2])
        words = _tokens(window)
        if not words:
            continue
        # Recall is intentional: source wording may refute the claim while still
        # sharing its named entities, quantities, and action.
        score = len(claim_tokens & words) / len(claim_tokens)
        if score > best_score:
            best_score, best = score, window
    relevance = "direct" if best_score >= 0.72 else "candidate" if best_score >= 0.38 else "none"
    return {"relevance": relevance, "score": round(best_score, 3), "excerpt": re.sub(r"\s+", " ", best).strip()[:1200] if relevance != "none" else ""}


def content_overlap(source_text: str, essay_text: str) -> float:
    """Estimate article-level overlap; quoted claims alone cannot trigger a reprint."""
    source_words = re.findall(r"[a-z0-9]+", _normalise_text(source_text))
    essay_words = re.findall(r"[a-z0-9]+", _normalise_text(essay_text))
    if len(source_words) < 120 or len(essay_words) < 120:
        return 0.0
    source_grams = {" ".join(source_words[index : index + 5]) for index in range(len(source_words) - 4)}
    essay_grams = {" ".join(essay_words[index : index + 5]) for index in range(len(essay_words) - 4)}
    if not source_grams or not essay_grams:
        return 0.0
    overlap = len(source_grams & essay_grams)
    return round(overlap / min(len(source_grams), len(essay_grams)), 4)


class SourcePolicy:
    """The versioned source-tier policy, applied in code rather than prompt text."""

    def __init__(self, path: Path = DEFAULT_POLICY_PATH) -> None:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        self.tiers = {int(key): value for key, value in (raw.get("tiers") or {}).items()}
        self.independence = raw.get("independence") or {}
        self.retrieval = raw.get("retrieval") or {}

    def classify(self, url: str) -> tuple[int, bool, str]:
        host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
        for tier, definition in sorted(self.tiers.items()):
            match = definition.get("match") or {}
            for pattern in match.get("hosts") or []:
                pattern = str(pattern).lower()
                if (pattern.startswith(".") and host.endswith(pattern)) or host == pattern or host.endswith("." + pattern):
                    return tier, bool(definition.get("can_support")), str(definition.get("name") or "unknown")
        fallback = self.tiers.get(5, {})
        return 5, bool(fallback.get("can_support", False)), str(fallback.get("name") or "other")

    def title_indicates_reprint(self, title: str) -> bool:
        tier_zero = self.tiers.get(0, {})
        titles = ((tier_zero.get("match") or {}).get("title_contains") or [])
        lowered = title.lower()
        return any(str(item).lower() in lowered for item in titles)

    @property
    def min_sources(self) -> int:
        return int(self.independence.get("min_sources", 2))

    @property
    def max_redirects(self) -> int:
        return int(self.retrieval.get("max_redirects", MAX_REDIRECTS))

    @property
    def max_document_bytes(self) -> int:
        return int(self.retrieval.get("max_document_bytes", MAX_DOCUMENT_BYTES))

    @property
    def accepted_content_types(self) -> tuple[str, ...]:
        return tuple(self.retrieval.get("accepted_content_types", TEXT_CONTENT_TYPES))


def independent_hits(hits: list[dict]) -> list[dict]:
    """Verified hits (see SourceVerifier.verify) that are eligible to support a verdict,
    deduped to one per publisher domain. Shared by every entry point (CLI pipeline, web UI)
    so "what counts as independent evidence" is defined once.
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


def cap_single_source_confidence(confidence: float, independent: list[dict], policy: SourcePolicy, scale: float = 1.0) -> float:
    """Cap confidence when a verdict rests on exactly one (still-eligible) source.

    Call only after `sufficient_evidence` has passed. `scale` converts the policy's
    0-1 cap to a caller's own confidence scale (e.g. 100 for a 0-100 scale).
    """
    if len(independent) >= policy.min_sources:
        return confidence
    cap = float(policy.independence.get("single_source_confidence_cap", 0.0)) * scale
    return min(confidence, cap)


class SourceVerifier:
    """Fetch pages with SSRF/redirect/content limits and create evidence records."""

    def __init__(
        self,
        policy: SourcePolicy | None = None,
        session: requests.Session | None = None,
        respect_robots: bool | None = None,
    ) -> None:
        self.policy = policy or SourcePolicy()
        self.session = session or self._new_session()
        self.respect_robots = bool(self.policy.retrieval.get("respect_robots_txt", True)) if respect_robots is None else respect_robots
        self._robots_cache: dict[str, list[tuple[str, bool]]] = {}

    @staticmethod
    def _new_session() -> requests.Session:
        session = requests.Session()
        retry = Retry(total=2, connect=2, read=2, status=2, backoff_factor=0.4, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"GET"}))
        session.mount("https://", HTTPAdapter(max_retries=retry))
        session.mount("http://", HTTPAdapter(max_retries=retry))
        session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1"})
        return session

    def _robots_allowed(self, url: str) -> bool:
        """Apply the relevant Allow/Disallow rules, failing open if robots is unavailable."""
        if not self.respect_robots:
            return True
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots_cache:
            rules: list[tuple[str, bool]] = []
            try:
                response = self.session.get(origin + "/robots.txt", timeout=(5, 10), allow_redirects=False, stream=True)
                content_type = response.headers.get("Content-Type", "")
                if response.status_code == 200 and (not content_type or "text" in content_type):
                    body = b"".join(response.iter_content(chunk_size=8192))[:65_536].decode("utf-8", errors="replace")
                    applies = False
                    for raw_line in body.splitlines():
                        line = raw_line.split("#", 1)[0].strip()
                        if not line or ":" not in line:
                            continue
                        key, value = (item.strip() for item in line.split(":", 1))
                        key = key.lower()
                        if key == "user-agent":
                            applies = value.lower() in {"*", "tapia-claim-checker"}
                        elif applies and key in {"allow", "disallow"} and value:
                            rules.append((value, key == "allow"))
                response.close()
            except requests.RequestException:
                # A temporary robots outage should not silently make every claim
                # unverifiable. This event remains observable in the source ledger.
                pass
            self._robots_cache[origin] = rules
        path = parsed.path or "/"
        matches = [(prefix, allowed) for prefix, allowed in self._robots_cache[origin] if path.startswith(prefix)]
        if not matches:
            return True
        longest = max(len(prefix) for prefix, _ in matches)
        return any(allowed for prefix, allowed in matches if len(prefix) == longest)

    def _fetch(self, requested_url: str) -> dict[str, Any]:
        try:
            current = canonicalize_url(requested_url)
        except UnsafeSourceUrl as exc:
            return {"status": "blocked_url", "error": str(exc), "requested_url": requested_url}
        redirects: list[str] = []
        try:
            for _ in range(self.policy.max_redirects + 1):
                if not public_hostname(current):
                    return {"status": "blocked_url", "error": "URL resolves to a non-public address", "requested_url": requested_url, "final_url": current, "redirects": redirects}
                if not self._robots_allowed(current):
                    return {"status": "blocked_robots", "error": "publisher robots.txt disallows this path", "requested_url": requested_url, "final_url": current, "redirects": redirects}
                response = self.session.get(current, timeout=(5, 20), stream=True, allow_redirects=False)
                if response.is_redirect or response.is_permanent_redirect:
                    location = response.headers.get("Location")
                    response.close()
                    if not location:
                        return {"status": "fetch_error", "error": "redirect missing Location", "requested_url": requested_url, "final_url": current, "redirects": redirects}
                    redirects.append(current)
                    current = canonicalize_url(urljoin(current, location))
                    continue
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if response.status_code != 200:
                    response.close()
                    return {"status": "http_error", "http_status": response.status_code, "requested_url": requested_url, "final_url": current, "redirects": redirects}
                if content_type not in self.policy.accepted_content_types:
                    response.close()
                    return {"status": "unsupported_content", "content_type": content_type, "requested_url": requested_url, "final_url": current, "redirects": redirects}
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_content(chunk_size=32_768):
                    total += len(chunk)
                    if total > self.policy.max_document_bytes:
                        response.close()
                        return {"status": "document_too_large", "requested_url": requested_url, "final_url": current, "redirects": redirects}
                    chunks.append(chunk)
                encoding = response.encoding or "utf-8"
                headers = {key.lower(): value for key, value in response.headers.items() if key.lower() in {"content-type", "etag", "last-modified", "date"}}
                response.close()
                return {"status": "retrieved", "requested_url": requested_url, "final_url": current, "redirects": redirects, "content_type": content_type, "headers": headers, "body": b"".join(chunks).decode(encoding, errors="replace")}
        except (requests.RequestException, UnicodeError, ValueError) as exc:
            return {"status": "fetch_error", "error": str(exc)[:300], "requested_url": requested_url, "final_url": current, "redirects": redirects}
        return {"status": "too_many_redirects", "requested_url": requested_url, "final_url": current, "redirects": redirects}

    @staticmethod
    def _extract_document(body: str, content_type: str) -> dict[str, str]:
        if content_type == "text/plain":
            text = re.sub(r"\s+", " ", body).strip()
            return {"title": "", "text": text, "published_at": ""}
        soup = BeautifulSoup(body, "html.parser")
        title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
        published = ""
        for selector in (("property", "article:published_time"), ("name", "date"), ("name", "publish-date")):
            tag = soup.find("meta", attrs={selector[0]: selector[1]})
            if tag and tag.get("content"):
                published = str(tag["content"]).strip()
                break
        for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "aside", "form"]):
            tag.decompose()
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
        return {"title": title, "text": text, "published_at": published}

    def verify(self, hit: dict[str, Any], claim: str, essay_text: str = "", essay_url: str = "") -> dict[str, Any]:
        fetched = self._fetch(str(hit.get("url") or ""))
        retrieved_at = utc_now()
        result: dict[str, Any] = {key: value for key, value in fetched.items() if key != "body"}
        result["retrieved_at"] = retrieved_at
        final_url = str(fetched.get("final_url") or hit.get("url") or "")
        if fetched.get("status") != "retrieved":
            result.update({"reprint": False, "reprint_overlap": 0.0, "relevance": "none", "match_score": 0.0, "evidence_excerpt": "", "eligible": False})
            return result
        document = self._extract_document(str(fetched["body"]), str(fetched["content_type"]))
        overlap = content_overlap(document["text"], essay_text) if essay_text else 0.0
        title_lc = document["title"].lower()
        canonical_essay = ""
        try:
            canonical_essay = canonicalize_url(essay_url) if essay_url else ""
        except UnsafeSourceUrl:
            pass
        reprint = bool(canonical_essay and final_url == canonical_essay) or self.policy.title_indicates_reprint(title_lc) or overlap >= 0.20
        tier, can_support, tier_name = self.policy.classify(final_url)
        evidence = find_claim_evidence(claim, document["text"])
        result.update(
            {
                "status": "verified",
                "final_url": final_url,
                "canonical_url": final_url,
                "title": document["title"] or str(hit.get("title") or ""),
                "published_at": document["published_at"],
                "content_sha256": hashlib.sha256(document["text"].encode("utf-8")).hexdigest(),
                "content_characters": len(document["text"]),
                "tier": tier,
                "tier_name": tier_name,
                "can_support": can_support,
                "publisher": registered_host(final_url),
                "reprint": reprint,
                "reprint_overlap": overlap,
                "relevance": evidence["relevance"],
                "match_score": evidence["score"],
                "evidence_excerpt": evidence["excerpt"],
                "eligible": bool(can_support and not reprint and evidence["relevance"] != "none"),
            }
        )
        return result
