# Pipeline

```text
essay
  → 01 extract   claims.json
  → 02 classify  classified.json     # only "verifiable" proceeds
  → 03 search    evidence.json       # every URL → notes/sources_<run>.log
  → 04 compare   verdicts.json       # supported / contradicted / misleading / …
  → 05 annotate  annotated.html
  → verify       human manual_review.md + error_log.md
```

Classification (brief’s three labels) is **not** the badge.

| Axis | Values | Question |
|---|---|---|
| Classification | supported_in_text / verifiable / opinion | Should we search? |
| Verdict | supported / contradicted / **misleading** / unverifiable / opinion | What badge? |

`misleading` is required: METR’s ~5 hour figure is a 50% success time horizon.

Tier 0 / reprints of this essay cannot support a claim. That is the c04 bug.

The second-pass `verify/verifier_agent.py` only writes challenges. Humans own
the error log.
