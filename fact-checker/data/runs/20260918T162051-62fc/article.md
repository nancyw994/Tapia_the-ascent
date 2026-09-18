# Claim Checker: Agent Pipeline

## 1. Upload and display

The user uploads a `.txt` article. The full article opens in the left panel, where sentences can later be highlighted and annotated.

## 2. Extract claim sentences

The agent identifies every sentence that makes a factual, testable claim. It returns each claim sentence and a short explanation of why it was selected. The explanations appear in the right panel.

```

## 3. User proceeds

The user reviews the extracted claims and clicks **Next**.

## 4. Classify each claim

The agent assigns each claim one type:

* **Scientific / general claim** — a testable statement about the world, technology, society, or a measurable trend.
* **Event claim** — a statement about a specific event, organization, person, release, date, or announcement.

The article remains on the left, with claim sentences highlighted in different colors by type. The right panel shows the classification rationale.

```

## 5. User proceeds

The user clicks **Next** to begin evidence retrieval.

## 6. Find and rank external sources

For each claim, the agent searches for relevant external sources. It returns the publisher, publication date, URL, relevance score (`-100` to `100`), and a short explanation. The interface displays sources with a relevance-color gradient: low/contradictory relevance in red, neutral in gray, and highly relevant supporting evidence in green.

```

## 7. User proceeds

The user reviews the retrieved evidence and clicks **Next**.

## 8. Produce a final verdict

The agent compares each claim against the evidence and returns a final verdict, confidence score, and concise reasoning. The main labels are **FAKE** and **NOT FAKE**; use **INSUFFICIENT EVIDENCE** when the available sources do not justify either conclusion.


The final view shows the annotated article on the left and the claim verdict, evidence, confidence, and reasoning on the right. The team should manually verify several verdicts, including at least one incorrect agent judgment, and document how it was caught.

