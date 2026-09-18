# Error log — claims the agent got wrong, and how a human caught it

Show this to judges. A team that says “it got everything right” has not looked.

## Wrong call (demo this)

**c04.** “I am no longer needed for the actual technical work of my job…”

- Agent: `verifiable` + **supported @ 0.95**
- Human (gold g02): anecdote, unverifiable
- Caught by: teammate compared `human_gold.json` (written first) to
  `verdicts.json`. Both “sources” are Fortune and CACM quoting the same essay.
  Reprint ≠ independent confirmation of Shumer’s Monday.

Same bug: **c06** supported @ 1.0 from a Business Insider excerpt of the essay.

## Missed

Extractor stopped at 10 claims and never reached:

- OpenAI “instrumental in creating itself” (gold g09)
- Amodei 50% entry-level jobs (gold g10)
- 2023 bar exam line (gold g07)
- “Free version is over a year behind” (gold g05)

## Invented / mis-typed

- **c02** rhetoric about a virus overseas treated as verifiable; `why` is the
  leftover string `"one sentence"`.
- **c10** “There’s an organization called METR” marked unverifiable after a
  failed search. METR is real; retrieval miss.

## Pitch line

The agent did fetch live pages and even caught the METR caveat. It still
stamped a personal anecdote as supported because other websites reprinted
the essay. A human had already labeled that line as anecdote.
