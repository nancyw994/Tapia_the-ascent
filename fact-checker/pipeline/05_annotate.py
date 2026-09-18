#!/usr/bin/env python3
"""Stage 5: verdicts.json + essay → annotated.html and annotated.md"""

from __future__ import annotations

import argparse
import html
import json
import re
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


def _coverage_line(coverage: dict) -> str:
    if not coverage:
        return ""
    return (
        f" · {int(coverage.get('claims_found') or 0)} found, "
        f"{int(coverage.get('claims_checked') or 0)} checked, "
        f"{int(coverage.get('reprints_dropped') or 0)} reprints dropped, "
        f"{int(coverage.get('pending_review') or 0)} pending review"
    )


def payload_from_web(
    *,
    essay_id: str,
    claims: list[dict],
    evidence: list[dict],
    verdicts: list[dict],
    coverage: dict | None = None,
    generated_at: str = "",
) -> dict:
    """Map a web session's step JSON onto the CLI annotate payload."""
    ev_by = {row.get("claim_id"): row for row in evidence or []}
    claim_by = {row.get("claim_id"): row for row in claims or []}
    out: list[dict] = []
    for row in verdicts or []:
        cid = row.get("claim_id")
        sources = (ev_by.get(cid) or {}).get("sources") or []
        cited = set(row.get("supporting_source_ids") or [])
        picked = [src for src in sources if src.get("source_id") in cited] or sources[:4]
        override = row.get("human_override") or {}
        why = str(row.get("reason") or "")
        if override:
            agent_v = override.get("agent_verdict") or row.get("agent_verdict") or ""
            agent_r = override.get("agent_reason") or row.get("agent_reason") or ""
            why = (
                f"Human override ({agent_v} → {override.get('verdict')}): "
                f"{override.get('why')}. Agent: {agent_r}"
            )
        try:
            conf = float(row.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf > 1:
            conf = conf / 100.0
        out.append(
            {
                "id": cid,
                "quote": (claim_by.get(cid) or {}).get("sentence") or "",
                "verdict": row.get("verdict") or "unverifiable",
                "confidence": conf,
                "label": "human" if override else "",
                "why": why,
                "sources": [
                    {"url": src.get("url"), "title": src.get("title"), "quote": src.get("snippet") or ""}
                    for src in picked
                ],
            }
        )
    return {
        "essay_id": essay_id,
        "agent": "claim-checker",
        "generated_at": generated_at,
        "coverage": coverage or {},
        "claims": out,
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


def _pdf_text(value: object) -> str:
    text = str(value or "")
    text = (
        text.replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u2014", "--")
        .replace("\u2013", "-")
        .replace("\u2026", "...")
        .replace("\u00a0", " ")
    )
    return re.sub(r"[^\x09\x0a\x0d\x20-\x7e]", "?", text)


def _pdf_rgb(hex_color: str) -> tuple[int, int, int]:
    raw = (hex_color or "#475467").lstrip("#")
    if len(raw) != 6:
        raw = "475467"
    return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)


def build_pdf(essay: str, payload: dict) -> bytes:
    """Session report PDF: coverage, claim cards, then the article text."""
    try:
        from fpdf import FPDF
    except ImportError as exc:
        raise RuntimeError("PDF export needs fpdf2. Run: pip install fpdf2") from exc

    class Report(FPDF):
        def footer(self) -> None:
            self.set_y(-12)
            self.set_font("Helvetica", "", 8)
            self.set_text_color(102, 112, 133)
            self.set_x(self.l_margin)
            self.cell(self.epw, 8, f"Page {self.page_no()}  |  badges are claims, not a newsroom verdict", align="C")

    pdf = Report(format="Letter")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.set_margins(16, 16, 16)

    title = _pdf_text(payload.get("essay_id") or "Annotated essay")
    def write(height: float, text: str) -> None:
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(pdf.epw, height, text or " ")

    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(16, 24, 40)
    write(8, title)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(102, 112, 133)
    write(
        5,
        _pdf_text(
            f"Agent: {payload.get('agent') or 'claim-checker'}  |  {payload.get('generated_at') or ''}"
            f"{_coverage_line(payload.get('coverage') or {})}"
        ),
    )
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 9)
    write(5, "Coverage of this run, not a fake-news score for the article.")
    pdf.ln(3)

    for claim in payload.get("claims") or []:
        verdict = claim.get("verdict") or "unverifiable"
        label = VERDICT_LABELS.get(verdict, str(verdict).upper())
        r, g, b = _pdf_rgb(VERDICT_COLORS.get(verdict, "#475467"))
        if pdf.get_y() > 250:
            pdf.add_page()
        pdf.set_fill_color(r, g, b)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 9)
        human = "  HUMAN" if claim.get("label") == "human" else ""
        try:
            conf = f"{float(claim.get('confidence') or 0):.0%}"
        except (TypeError, ValueError):
            conf = "--"
        pdf.set_x(pdf.l_margin)
        pdf.cell(pdf.epw, 7, _pdf_text(f"  {claim.get('id')}  {label}{human}  {conf}"), fill=True)
        pdf.ln(8)
        pdf.set_text_color(16, 24, 40)
        pdf.set_font("Helvetica", "I", 11)
        write(6, _pdf_text(f'"{claim.get("quote") or ""}"'))
        pdf.set_font("Helvetica", "", 10)
        write(5, _pdf_text(claim.get("why") or ""))
        for src in claim.get("sources") or []:
            pdf.set_font("Helvetica", "", 8)
            pdf.set_text_color(21, 94, 239)
            write(4, _pdf_text(src.get("title") or src.get("url") or ""))
            pdf.set_text_color(71, 84, 103)
            if src.get("url"):
                write(4, _pdf_text(src.get("url")))
            if src.get("quote"):
                write(4, _pdf_text(src.get("quote")))
        pdf.set_text_color(16, 24, 40)
        pdf.ln(3)

    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    write(8, "Article")
    pdf.set_font("Helvetica", "", 10)
    write(5, _pdf_text(essay or ""))
    return bytes(pdf.output())


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
    coverage_line = _coverage_line(payload.get("coverage") or {})
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
    <p>Agent: {agent} · {generated} · badges are claims, not a newsroom verdict{coverage_line}</p>
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
