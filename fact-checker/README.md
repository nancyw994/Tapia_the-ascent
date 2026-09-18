# Fact checker (TAPIA Challenge 11)

Extract → classify → search → compare → annotate, then a human shows where
the agent was wrong. Not a newsroom product.

## Conflicts we fixed vs the brief’s tree

1. **Three brief labels are classification only.** Badges also use
   `misleading` (see `config/claim_labels.yaml`).
2. **Reprints of this essay are tier 0** and cannot support a verdict
   (`config/source_tiers.yaml`). That is how c04 went wrong.
3. **`verifier_agent.py` does not replace humans.** Ground truth is
   `verify/manual_review.md` and `verify/error_log.md`.

## Demo

Open `data/runs/2026-09-18-qwen25-3b/annotated.html`.

Pitch: `docs/judges_summary.md`. Human catch: `verify/error_log.md`.

## Run

From this directory (needs Ollama `qwen2.5:3b`):

```bash
python3 pipeline/run.py --essay data/essays/shumer_2026-02-09.md
```

Optional second pass (writes challenges, does not edit verdicts):

```bash
python3 verify/verifier_agent.py \
  --verdicts data/runs/<run_id>/verdicts.json \
  --evidence data/runs/<run_id>/evidence.json \
  --out data/runs/<run_id>/verifier_challenges.json
```
