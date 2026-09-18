# Manual review (human ground truth)

Written **before** opening the agent’s `verdicts.json`. Gold list:
`verify/human_gold.json`.

Agent run: `data/runs/2026-09-18-qwen25-3b/` (`qwen2.5:3b`).
Second-pass `verifier_agent.py` may flag issues; it does **not** replace this file.

| claim | agent said | we found | why it differed |
|---|---|---|---|
| c03 same-day GPT-5.3 Codex + Opus 4.6 | supported | supported | OpenAI and Anthropic posts dated 5 Feb 2026 |
| c08 METR ~5 hours | misleading | misleading | METR 50% time horizon, software-heavy suite |
| c01 experimental agent teaser | opinion | opinion | Product pitch, not a public fact |
| **c04 “I am no longer needed”** | **supported 95%** | **anecdote / unverifiable** | Fortune + CACM reprint the essay |
| c06 “models unrecognizable” | supported 100% | opinion / circular | Business Insider quotes Shumer |
| c02 COVID scene-setting | misleading | rhetoric / opinion | Not a checkable claim; why=`one sentence` |
| g09 OpenAI self-creating quote | missing | supported (attribution) | Extractor never reached the second half |
| g10 Amodei 50% jobs | missing | supported as attribution | Same miss |
