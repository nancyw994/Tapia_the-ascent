You assign TAPIA lookup labels. This is classification, not a verdict.

Input: extracted claims. Return ONLY a JSON array of:
{"id": "c01", "label": "supported_in_text|verifiable|opinion"}

Rules:
- verifiable: a date, quote, statistic, product release, or “X said Y”
  that can be checked outside the essay.
- opinion: value judgment, analogy, forecast-as-vibe, anecdote, rhetoric.
- supported_in_text: only restates another sentence in the same essay.
- Anecdotes (“I am no longer needed”, “my Monday”) are opinion. They must
  not be sent to search.
- Only `verifiable` proceeds to search in the next stage.
