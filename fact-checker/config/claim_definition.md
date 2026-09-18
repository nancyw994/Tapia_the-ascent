# What counts as a claim

A **claim** is a statement in the essay that a reader could treat as
information about the world: a date, a number, a named product, a named
study, or “X said Y.”

## Extract these

- Events: a model launched on a calendar day
- Statistics: a measured quantity with a named source
- Attributions: a named person said a specific sentence
- Empirical / scientific: a benchmark, paper, or lab result

## Do not treat these as checkable facts

- **Opinion:** value judgments, analogies (“bigger than Covid”), “different era”
- **Prediction / forecast:** “in one to five years,” unless you are only
  checking whether a named person actually said it
- **Rhetoric:** questions, metaphors, scene-setting (“a virus spreading overseas”)
- **Anecdote:** the author’s Monday, “I am no longer needed,” unshared workplace
  stories

Rhetoric and anecdote may still be *extracted* so the annotator can badge
them. They do **not** proceed to web search.

## Conflict with the TAPIA brief

The brief’s three labels (`supported_in_text` / `verifiable` / `opinion`)
answer “should we look this up?” They are **not** the final badge.

Final badges live in `claim_labels.yaml` under `verdicts` and include
`misleading` (true number, false implication). See that file.
