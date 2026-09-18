# Source verification operations

This repository verifies **claims**, not publishers or whole documents. A final
article-level trust score must be derived from the claim verdicts and always show
its coverage: claims found, claims checked, evidence quality, and claims needing
human review. Never present a binary "fake news" label as a factual finding.

## What the pipeline guarantees

`pipeline/source_verification.py` makes a search result eligible only after it
has a retrieval record. That record includes the requested and final URL,
redirect chain, timestamp, MIME type, source hash, publisher key, policy tier,
reprint decision, relevance score, and the extracted passage given to the LLM.
The original search snippet is retained as discovery metadata but is never given
to the comparator as evidence.

The verifier rejects malformed URLs, URL credentials, loopback/private/reserved
addresses, redirects through those addresses, unsupported content types, files
larger than 2 MB, and pages disallowed by robots.txt. It uses bounded retries and
does not execute JavaScript or download attachments.

## Required deployment controls

Application-level checks cannot fully prevent DNS rebinding. In production, run
retrieval in a separate non-privileged worker behind an egress proxy/firewall
that denies RFC1918, loopback, link-local, metadata-service, and internal DNS
ranges at connection time. The worker should have no credentials, no access to
application databases except its queue, a strict per-host rate limit, and a
short-lived content cache keyed by canonical URL plus ETag/Last-Modified.

Keep the source-tier policy under review. A tier is a reliability prior, not a
truth guarantee: first-party sources are strong for what an organization
announced, but weak for a contested claim about itself. New or disputed verdicts,
low-quality evidence, policy changes, and all high-impact topics should enter a
human-review queue.

## Evidence retention and privacy

Persist compact excerpts and SHA-256 hashes by default, not arbitrary full pages
or uploaded documents. Encrypt uploaded documents at rest, use expiring object
storage for original files, redact secrets before logging, and provide deletion
controls. Record the policy version and retrieval timestamp with every verdict so
that a result can be reproduced or challenged later.

## Acceptance checks

Before exposing a public trust score, run an adversarial corpus covering:

- reprints, syndicated articles, and circular citations;
- a claim quoted in a legitimate primary source (must not be mistaken for a
  reprint);
- redirects to private addresses and oversized/non-HTML payloads;
- sources that mention a claim but contradict it;
- stale, withdrawn, paywalled, and robots-disallowed sources; and
- balanced examples of supported, contradicted, misleading, opinion, and
  insufficient-evidence claims.

Track precision and recall per verdict, source-quality distribution, retrieval
failure rate, and human-overturn rate. A high uncertainty or low-coverage result
must say "insufficient evidence", not "trustworthy".
