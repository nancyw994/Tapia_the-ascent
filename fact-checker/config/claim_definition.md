# What Counts as a Claim

This document defines what the extractor pulls out of an essay and what it leaves alone.
Every downstream stage assumes these definitions.

## Definition

A **claim** is a statement about the world that could, in principle, be shown true or false
by evidence that exists independently of the author. The test is: *could a careful reader
go and check this?*

A sentence may contain more than one claim. A claim may span more than one sentence when the
second sentence only supplies a number or a name for the first.

## Claim vs. not a claim

| Category | Extract? | Why |
|---|---|---|
| Factual assertion | Yes | Checkable against external evidence |
| Statistic or number | Yes | Checkable against the cited or original source |
| Attribution ("X said Y") | Yes | Checkable: did X say Y, and in what context |
| Description of a past event | Yes | Checkable against reporting or records |
| Forecast or prediction | Yes, as `forecast` | Not yet checkable, but the premises usually are |
| Opinion or value judgement | No | No fact of the matter to verify |
| Rhetorical question | No | Not an assertion |
| Hedge, framing, transition | No | Carries no checkable content |
| Definition the author sets up | No | True by stipulation within the essay |

## Opinion

A statement is an **opinion** when it expresses preference, evaluation, or judgement rather
than a state of affairs. Markers: "I think", "should", "better", "important", "impressive",
"worrying". An opinion wrapped around a fact still contains the fact. Extract the fact, drop
the wrapper.

Example:
- "It is remarkable that the model scored 90% on the benchmark."
- Extract: "the model scored 90% on the benchmark". Drop: "it is remarkable".

## Prediction

A statement about the future is a **forecast**. It cannot be verified now, but the pipeline
still extracts it because:
1. Forecasts often rest on a stated premise that *is* checkable.
2. The annotator should badge forecasts so readers do not mistake them for facts.

Forecasts are classified as `opinion` with `type: forecast` and receive the `forecast` badge.
They are never sent to search.

## Rhetoric

Rhetoric is language whose purpose is persuasion rather than description: analogies,
hyperbole, appeals to the reader, framing sentences. Rhetoric is not extracted even when it
sounds factual. "Everyone knows that..." is rhetoric; the clause after it may be a claim.

## Edge cases

- **Vague quantities** ("many experts", "most companies"): extract as a claim of type
  `statistic` and let the classifier decide whether it is specific enough to verify.
- **Claims cited to a source inside the essay** ("according to the Fed, inflation was 3.1%"):
  extract as `attribution` plus `statistic`. The attribution is checked against the source;
  the statistic is checked against the world.
- **Self-referential claims** ("I tested this myself"): extract, label `opinion` unless the
  essay includes checkable details.
- **Claims about the essay's own argument** ("as I showed above"): not extracted.
- **Compound claims** ("A and B"): split into two claims with the same location.
- **Negations** ("X never happened"): extract as stated. The comparator handles polarity.

## Location

Every claim records where it came from: paragraph index, sentence index, and character
offsets into the essay file. The annotator uses these to place badges. If a claim was
assembled from two sentences, the location covers both.
