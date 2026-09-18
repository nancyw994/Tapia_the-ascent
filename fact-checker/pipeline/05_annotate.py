#!/usr/bin/env python3
"""Stage 5: verdicts.json + essay → annotated.html and annotated.md"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

VERDICT_LABELS = {
    "supported": "SUPPORTED",
    "contradicted": "CONTRADICTED",
    "misleading": "MISLEADING",
    "unverifiable": "UNVERIFIABLE",
    "opinion": "OPINION",
}

VERDICT_COLORS = {
    "supported": "#0f7b4c",
    "contradicted": "#b42318",
    "misleading": "#b54708",
    "unverifiable": "#475467",
    "opinion": "#6941c6",
}


def highlight_essay(essay: str, claims: list[dict]) -> str:
    # Claims are not guaranteed to be in essay order, so locate each quote anywhere.
    spans: list[tuple[int, int, dict]] = []
    used: set[str] = set()
    for claim in claims:
        quote = (claim.get("quote") or "").strip()
        if not quote or quote in used:
            continue
        start = essay.find(quote)
        if start < 0:
            continue
        spans.append((start, start + len(quote), claim))
        used.add(quote)
    spans.sort(key=lambda item: item[0])
    parts: list[str] = []
    last = 0
    for start, end, claim in spans:
        if start < last:
            continue
        parts.append(html.escape(essay[last:start]))
        verdict = claim.get("verdict", "unverifiable")
        color = VERDICT_COLORS.get(verdict, "#475467")
        cid = html.escape(str(claim.get("id", "")))
        inner = html.escape(essay[start:end])
        parts.append(
            f'<mark class="q {html.escape(verdict)}" id="q-{cid}" '
            f'style="background:{html.escape(color)}22;border-bottom:2px solid {html.escape(color)}">'
            f"{inner}<a class=\"badge-link\" href=\"#c-{cid}\">{html.escape(VERDICT_LABELS.get(verdict, verdict))}</a>"
            "</mark>"
        )
        last = end
    parts.append(html.escape(essay[last:]))
    return "".join(parts).replace("\n", "<br>\n")


def render_sources(sources: list[dict]) -> str:
    if not sources:
        return "<p class='muted'>No sources logged.</p>"
    items = []
    for src in sources:
        url = html.escape(src.get("url") or "")
        title = html.escape(src.get("title") or url)
        quote = html.escape(src.get("quote") or "")
        qhtml = f"<blockquote>{quote}</blockquote>" if quote else ""
        items.append(f'<li><a href="{url}">{title}</a>{qhtml}</li>')
    return "<ul>" + "".join(items) + "</ul>"


def render_claim_card(claim: dict) -> str:
    verdict = claim.get("verdict", "unverifiable")
    color = VERDICT_COLORS.get(verdict, "#475467")
    cid = html.escape(str(claim.get("id", "")))
    label = html.escape(VERDICT_LABELS.get(verdict, verdict))
    claim_type = html.escape(str(claim.get("label") or claim.get("claim_type") or ""))
    try:
        conf_txt = f"{float(claim.get('confidence')):.0%}"
    except (TypeError, ValueError):
        conf_txt = "—"
    quote = html.escape(claim.get("quote") or "")
    why = html.escape(claim.get("why") or "")
    return f"""
<article class="card" id="c-{cid}">
  <header>
    <span class="pill" style="background:{color}">{label}</span>
    <span class="meta">{claim_type} · confidence {conf_txt} · {cid}</span>
  </header>
  <p class="quote">“{quote}”</p>
  <p>{why}</p>
  {render_sources(claim.get("sources") or [])}
</article>
"""


def build_html(essay: str, payload: dict) -> str:
    claims = payload.get("claims") or []
    counts: dict[str, int] = {}
    for claim in claims:
        key = claim.get("verdict") or "unverifiable"
        counts[key] = counts.get(key, 0) + 1
    legend = "".join(
        f'<li><span class="dot" style="background:{VERDICT_COLORS.get(k, "#475467")}"></span>'
        f"{html.escape(VERDICT_LABELS.get(k, k))} ({v})</li>"
        for k, v in counts.items()
    )
    cards = "\n".join(render_claim_card(c) for c in claims)
    highlighted = highlight_essay(essay, claims)
    title = html.escape(payload.get("essay_id") or "Annotated essay")
    agent = html.escape(payload.get("agent") or "claim-checker")
    generated = html.escape(payload.get("generated_at") or "")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} — claim checker</title>
  <style>
    :root {{ font-family: Georgia, "Times New Roman", serif; color: #101828; }}
    body {{ margin: 0; background: #f8f6f1; }}
    header.top {{ background: #101828; color: #fff; padding: 1.25rem 1.5rem; }}
    header.top p {{ margin: 0.25rem 0 0; opacity: 0.8; font-family: ui-sans-serif, system-ui; font-size: 0.9rem; }}
    main {{ display: grid; grid-template-columns: minmax(0, 1.3fr) minmax(280px, 0.9fr); gap: 1.5rem; padding: 1.5rem; }}
    @media (max-width: 900px) {{ main {{ grid-template-columns: 1fr; }} }}
    .essay, .rail {{ background: #fff; padding: 1.25rem 1.4rem; border-radius: 12px; box-shadow: 0 1px 3px #00000014; }}
    .legend {{ display: flex; flex-wrap: wrap; gap: 0.75rem; list-style: none; padding: 0; font-family: ui-sans-serif, system-ui; font-size: 0.85rem; }}
    .dot {{ display: inline-block; width: 0.7rem; height: 0.7rem; border-radius: 50%; margin-right: 0.35rem; }}
    mark.q {{ padding: 0 0.15rem; }}
    .badge-link {{ font-family: ui-sans-serif, system-ui; font-size: 0.7rem; font-weight: 700; margin-left: 0.35rem; text-decoration: none; color: inherit; }}
    .card {{ border-top: 1px solid #eaecf0; padding: 1rem 0; }}
    .pill {{ color: #fff; font-family: ui-sans-serif, system-ui; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.04em; padding: 0.2rem 0.5rem; border-radius: 999px; }}
    .meta {{ font-family: ui-sans-serif, system-ui; font-size: 0.75rem; color: #667085; margin-left: 0.5rem; }}
    .quote {{ font-style: italic; }}
    blockquote {{ margin: 0.4rem 0 0; color: #475467; font-size: 0.9rem; border-left: 3px solid #d0d5dd; padding-left: 0.6rem; }}
    .muted {{ color: #667085; }}
    a {{ color: #155eef; }}
  </style>
</head>
<body>
  <header class="top">
    <h1>{title}</h1>
    <p>Agent: {agent} · {generated} · badges are claims, not a newsroom verdict</p>
  </header>
  <main>
    <section class="essay">
      <ul class="legend">{legend}</ul>
      <div class="body">{highlighted}</div>
    </section>
    <aside class="rail">
      <h2>Claims</h2>
      {cards}
    </aside>
  </main>
</body>
</html>
"""


def build_markdown(essay: str, payload: dict) -> str:
    lines = [f"# {payload.get('essay_id')}", "", f"Agent: {payload.get('agent')}", ""]
    for claim in payload.get("claims") or []:
        badge = VERDICT_LABELS.get(claim.get("verdict"), claim.get("verdict"))
        lines.append(f"## {claim.get('id')} — {badge}")
        lines.append(f"> {claim.get('quote')}")
        lines.append("")
        lines.append(claim.get("why") or "")
        lines.append("")
    lines.append("## Essay")
    lines.append("")
    lines.append(essay)
    return "\n".join(lines) + "\n"


def run(essay_path: Path, verdicts_path: Path, html_out: Path, md_out: Path) -> None:
    essay = essay_path.read_text(encoding="utf-8")
    payload = json.loads(verdicts_path.read_text(encoding="utf-8"))
    if "claims" not in payload:
        raise ValueError(f"{verdicts_path} needs a top-level claims array")
    html_out.parent.mkdir(parents=True, exist_ok=True)
    html_out.write_text(build_html(essay, payload), encoding="utf-8")
    md_out.write_text(build_markdown(essay, payload), encoding="utf-8")
    print(f"annotate: {html_out}")
    print(f"annotate: {md_out}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--essay", type=Path, required=True)
    p.add_argument("--verdicts", type=Path, required=True)
    p.add_argument("--html", type=Path, required=True)
    p.add_argument("--md", type=Path, required=True)
    args = p.parse_args()
    run(args.essay, args.verdicts, args.html, args.md)


if __name__ == "__main__":
    main()
