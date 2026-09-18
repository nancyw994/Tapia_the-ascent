# TAPIA Claim Checker

Code lives in [`fact-checker/`](fact-checker/). That tree is extract → classify →
search → compare → annotate, then human verify.

**Open this for judges:**
[`fact-checker/data/runs/2026-09-18-qwen25-3b/annotated.html`](fact-checker/data/runs/2026-09-18-qwen25-3b/annotated.html)

Human catch: [`fact-checker/verify/error_log.md`](fact-checker/verify/error_log.md)  
Pitch: [`fact-checker/docs/judges_summary.md`](fact-checker/docs/judges_summary.md)

## Conflicts we fixed in the new layout

- Brief’s three labels = **classification** (search or not). Badges also include **`misleading`**.
- Reprints of the Shumer essay are **tier 0** and cannot support a claim.
- `verifier_agent.py` only challenges; it does not replace `manual_review.md`.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 fact-checker/pipeline/run.py
```
