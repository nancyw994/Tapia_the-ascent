---
name: claim-checker
description: Fact-check essay claims against live web sources.
version: 0.2.0
author: TAPIA team the-ascent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Research, FactCheck, Claims, Web]
    category: research
    requires_tools: [web_search, web_extract]
---

# Claim Checker

Follow `fact-checker/docs/pipeline.md`. Definitions are in
`fact-checker/config/` (not in this file).

Preferred command:

```bash
python3 fact-checker/pipeline/run.py --essay fact-checker/data/essays/shumer_2026-02-09.md
```

Then stop. Humans fill `fact-checker/verify/manual_review.md`. Do not rewrite
verdicts after a human disagrees.

Classification (search?) is `supported_in_text` / `verifiable` / `opinion`.
Verdict badges also include `misleading`. Reprints of this essay cannot support
a claim.
