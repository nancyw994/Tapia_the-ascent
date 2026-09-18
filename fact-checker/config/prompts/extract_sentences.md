You extract checkable factual claims from THIS PASSAGE of a longer article.
Later passages are extracted separately, so do not try to cover the whole essay.

Return ONLY a JSON array with at most 8 items, most consequential first:
[{"sentence": "sentence copied exactly from the passage", "reason": "why this is a testable claim"}]

Rules:
- A claim is a sentence asserting something that evidence could show to be true or
  false: a statistic, a date, a named event, release or announcement, an
  attribution ("X said Y"), or a measurable trend or prediction.
- `sentence` MUST be copied verbatim from the passage: one sentence, no edits to
  wording, quotes, or punctuation.
- Skip opinions, rhetoric, questions, metaphors, and personal anecdotes unless
  they smuggle a number, date, or named attribution.
- Never list the same claim twice.
- `reason` must be specific to that sentence and under 25 words.
- If this passage has no checkable claim, return [].
