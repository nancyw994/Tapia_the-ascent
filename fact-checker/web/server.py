#!/usr/bin/env python3
"""Local web UI for the step-by-step claim checker.

    python web/server.py            # http://127.0.0.1:8000

The page uploads a .md/.txt article or pastes a URL (fetched into article.md), then calls one step
at a time. Every step's result is saved to data/runs/<session_id>/.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import secrets
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

WEB = Path(__file__).resolve().parent
sys.path.insert(0, str(WEB.parent / "pipeline"))

import steps  # noqa: E402
from fetch_article import fetch_url_markdown  # noqa: E402
from lib import ROOT, now_iso, write_json  # noqa: E402

_anno_spec = importlib.util.spec_from_file_location("annotate_html", WEB.parent / "pipeline" / "05_annotate.py")
annotate = importlib.util.module_from_spec(_anno_spec)
assert _anno_spec.loader is not None
_anno_spec.loader.exec_module(annotate)

ARTICLE_TYPES = {".md", ".txt"}
MAX_BODY = 600_000
MIN_CHARS, MAX_CHARS = 200, 200_000
SAMPLE = ROOT / "data" / "essays" / "shumer_2026-02-09.md"
ERROR_LOG = ROOT / "verify" / "error_log.md"

# step name → (data key it produces, key it needs, file it is saved to)
STEP_INFO = {
    "extract": ("claims", None, "claims.json"),
    "classify": ("classifications", "claims", "classifications.json"),
    "sources": ("evidence", "classifications", "evidence.json"),
    "verdict": ("verdicts", "evidence", "verdicts.json"),
}
DATA_ORDER = [key for key, _, _ in STEP_INFO.values()]

lock = threading.Lock()
sessions: dict[str, dict] = {}
log_lock = threading.Lock()


class Server(ThreadingHTTPServer):
    allow_reuse_address = True


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def persist_verdicts(session: dict) -> dict:
    coverage = steps.article_coverage(session["data"].get("claims") or [], session["data"].get("evidence") or [], session["data"].get("verdicts") or [])
    session["data"]["coverage"] = coverage
    write_json(session["dir"] / "verdicts.json", {"verdicts": session["data"]["verdicts"], "coverage": coverage})
    write_json(
        session["dir"] / "overrides.json",
        [
            {"claim_id": row["claim_id"], **row["human_override"]}
            for row in session["data"]["verdicts"]
            if row.get("human_override")
        ],
    )
    return coverage


def append_human_catch(session: dict, claim: dict, agent_verdict: str, agent_conf, human_verdict: str, why: str) -> None:
    sentence = str(claim.get("sentence") or "")
    snippet = sentence if len(sentence) < 140 else sentence[:137] + "..."
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sid = session["dir"].name
    filename = session.get("filename") or "article.md"
    block = (
        f"\n## Session `{sid}` — {filename} ({stamp})\n\n"
        f"**{claim['claim_id']}.** “{snippet}”\n\n"
        f"- Agent: `{agent_verdict}` @ {agent_conf}%\n"
        f"- Human: `{human_verdict}`\n"
        f"- Why: {why}\n"
    )
    ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
    with log_lock:
        with ERROR_LOG.open("a", encoding="utf-8") as handle:
            handle.write(block)
        with (session["dir"] / "human_review.md").open("a", encoding="utf-8") as handle:
            handle.write(block)


def load_session(session_id: str) -> dict | None:
    with lock:
        if session_id in sessions:
            return sessions[session_id]
    run_dir = ROOT / "data" / "runs" / session_id
    article_path = run_dir / "article.md"
    if not article_path.is_file():
        return None
    data: dict = {}
    for key, _, filename in STEP_INFO.values():
        path = run_dir / filename
        if not path.is_file():
            continue
        payload = _read_json(path)
        data[key] = payload.get(key, payload)
        if key == "verdicts" and isinstance(payload, dict) and "coverage" in payload:
            data["coverage"] = payload["coverage"]
    meta = _read_json(run_dir / "source.json") if (run_dir / "source.json").is_file() else {}
    session = {
        "article": article_path.read_text(encoding="utf-8"),
        "essay_url": str(meta.get("url") or ""),
        "filename": str(meta.get("filename") or "article.md"),
        "data": data,
        "dir": run_dir,
        "log": ROOT / "notes" / f"sources_{session_id}.log",
        "running": False,
    }
    with lock:
        sessions[session_id] = session
    return session


def session_payload(session: dict) -> dict:
    data = session["data"]
    if "verdicts" not in data:
        raise ValueError("run verdicts before exporting")
    coverage = data.get("coverage") or steps.article_coverage(
        data.get("claims") or [], data.get("evidence") or [], data.get("verdicts") or []
    )
    return annotate.payload_from_web(
        essay_id=session.get("filename") or session["dir"].name,
        claims=data.get("claims") or [],
        evidence=data.get("evidence") or [],
        verdicts=data.get("verdicts") or [],
        coverage=coverage,
        generated_at=now_iso(),
    )


def export_html(session: dict) -> bytes:
    payload = session_payload(session)
    html = annotate.build_html(session["article"], payload)
    (session["dir"] / "annotated.html").write_text(html, encoding="utf-8")
    return html.encode("utf-8")


def export_pdf(session: dict) -> bytes:
    payload = session_payload(session)
    pdf = annotate.build_pdf(session["article"], payload)
    (session["dir"] / "annotated.pdf").write_bytes(pdf)
    return pdf


def run_step(session: dict, step: str) -> dict:
    key, needs, filename = STEP_INFO[step]
    data = session["data"]
    if needs and needs not in data:
        raise ValueError(f"run the earlier step first ({needs} is missing)")
    if step == "extract":
        result = steps.extract_claims(session["article"])
    elif step == "classify":
        result = steps.classify_claims(data["claims"])
    elif step == "sources":
        result = steps.find_sources(data["claims"], session["log"], essay_url=session.get("essay_url") or "")
    else:
        result = steps.decide_verdicts(data["claims"], data["evidence"])
    for later in DATA_ORDER[DATA_ORDER.index(key) :]:
        data.pop(later, None)  # a re-run invalidates everything downstream
    data.pop("coverage", None)
    data[key] = result
    if step == "verdict":
        coverage = persist_verdicts(session)
        return {"verdicts": result, "coverage": coverage}
    write_json(session["dir"] / filename, {key: result})
    return {key: result}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode())

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/sample" and SAMPLE.is_file():
            self._json(200, {"filename": SAMPLE.name, "text": SAMPLE.read_text(encoding="utf-8")})
        else:
            self._json(404, {"error": f"not found: GET {path}"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        # These calls spend API money: only accept requests from our own page, not other websites.
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in {"127.0.0.1", "localhost"}:
            return self._json(403, {"error": "forbidden origin"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._json(413, {"error": "article is too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "invalid JSON"})
        if path == "/api/upload":
            self._upload(body)
        elif path == "/api/from-url":
            self._from_url(body)
        elif path == "/api/step":
            self._step(body)
        elif path == "/api/override":
            self._override(body)
        elif path == "/api/claims":
            self._claims(body)
        elif path == "/api/export":
            self._export(body, kind="html")
        elif path == "/api/export-pdf":
            self._export(body, kind="pdf")
        else:
            self._json(404, {"error": f"not found: POST {path}. Restart the server if you just added a route."})

    def _start_session(self, filename: str, text: str, essay_url: str = "") -> None:
        if len(text.strip()) < MIN_CHARS:
            return self._json(400, {"error": f"the article is too short (need at least {MIN_CHARS} characters)"})
        if len(text) > MAX_CHARS:
            return self._json(413, {"error": f"the article is too long (limit {MAX_CHARS:,} characters)"})
        session_id = f"{datetime.now():%Y%m%dT%H%M%S}-{secrets.token_hex(2)}"
        run_dir = ROOT / "data" / "runs" / session_id
        run_dir.mkdir(parents=True, exist_ok=True)
        article_path = run_dir / "article.md"
        article_path.write_text(text, encoding="utf-8")
        write_json(run_dir / "source.json", {"url": essay_url, "filename": filename})
        with lock:
            sessions[session_id] = {
                "article": text,
                "essay_url": essay_url,
                "filename": filename,
                "data": {},
                "dir": run_dir,
                "log": ROOT / "notes" / f"sources_{session_id}.log",
                "running": False,
            }
        self._json(200, {"session_id": session_id, "filename": filename, "chars": len(text), "url": essay_url, "text": text})

    def _upload(self, body: dict) -> None:
        filename, text = str(body.get("filename") or ""), body.get("text")
        if Path(filename).suffix.lower() not in ARTICLE_TYPES:
            return self._json(400, {"error": "please upload a .md or .txt file"})
        if not isinstance(text, str):
            return self._json(400, {"error": "missing article text"})
        self._start_session(filename, text)

    def _from_url(self, body: dict) -> None:
        url = str(body.get("url") or "").strip()
        try:
            text, canonical = fetch_url_markdown(url)
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
        header = f"# Source\n\n{canonical}\n\n"
        self._start_session("article.md", header + text + "\n", essay_url=canonical)

    def _step(self, body: dict) -> None:
        step = body.get("step")
        session = load_session(str(body.get("session_id") or ""))
        if session is None or step not in STEP_INFO:
            return self._json(400, {"error": "unknown session or step"})
        with lock:
            if session["running"]:
                return self._json(409, {"error": "a step is already running for this article"})
            session["running"] = True
        try:
            self._json(200, run_step(session, step))
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except RuntimeError as exc:
            self._json(500, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            session["running"] = False

    def _save_claims(self, session: dict, claims: list) -> None:
        session["data"]["claims"] = claims
        for later in DATA_ORDER[DATA_ORDER.index("claims") + 1 :]:
            session["data"].pop(later, None)
        session["data"].pop("coverage", None)
        write_json(session["dir"] / "claims.json", {"claims": claims})
        for name in ("classifications.json", "evidence.json", "verdicts.json", "overrides.json"):
            path = session["dir"] / name
            if path.is_file():
                path.unlink()

    def _claims(self, body: dict) -> None:
        session = load_session(str(body.get("session_id") or ""))
        if session is None:
            return self._json(400, {"error": "unknown session"})
        if "claims" not in session["data"]:
            return self._json(400, {"error": "extract claims first"})
        action = str(body.get("action") or "")
        try:
            if action == "remove":
                claims = steps.drop_claim(session["article"], session["data"]["claims"], str(body.get("claim_id") or ""))
            elif action == "add":
                claims = steps.add_claim(
                    session["article"],
                    session["data"]["claims"],
                    str(body.get("sentence") or ""),
                    str(body.get("reason") or ""),
                )
            else:
                return self._json(400, {"error": "action must be add or remove"})
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        self._save_claims(session, claims)
        self._json(200, {"claims": claims})

    def _override(self, body: dict) -> None:
        session = load_session(str(body.get("session_id") or ""))
        if session is None:
            return self._json(400, {"error": "unknown session"})
        verdicts = session["data"].get("verdicts")
        if not verdicts:
            return self._json(400, {"error": "run verdicts before overriding"})
        claim_id = str(body.get("claim_id") or "")
        human = steps.normalize_verdict(body.get("verdict"))
        why = str(body.get("why") or "").strip()
        if human not in steps.VERDICTS:
            return self._json(400, {"error": "verdict must be supported, contradicted, misleading, unverifiable, or opinion"})
        if len(why) < 8:
            return self._json(400, {"error": "write one sentence explaining why the agent is wrong"})
        if len(why) > 500:
            return self._json(400, {"error": "override reason is too long"})
        claims = {row["claim_id"]: row for row in session["data"].get("claims") or []}
        claim = claims.get(claim_id)
        if claim is None:
            return self._json(400, {"error": "unknown claim"})
        row = next((item for item in verdicts if item.get("claim_id") == claim_id), None)
        if row is None:
            return self._json(400, {"error": "unknown claim"})
        agent_verdict = row.get("agent_verdict") or (row.get("human_override") or {}).get("agent_verdict") or row.get("verdict")
        agent_reason = row.get("agent_reason") or (row.get("human_override") or {}).get("agent_reason") or row.get("reason")
        agent_conf = row.get("confidence") or 0
        row["agent_verdict"] = agent_verdict
        row["agent_reason"] = agent_reason
        row["verdict"] = human
        row["reason"] = why
        row["human_override"] = {
            "verdict": human,
            "why": why,
            "at": now_iso(),
            "agent_verdict": agent_verdict,
            "agent_reason": agent_reason,
        }
        coverage = persist_verdicts(session)
        append_human_catch(session, claim, agent_verdict, agent_conf, human, why)
        self._json(200, {"verdicts": verdicts, "coverage": coverage})

    def _export(self, body: dict, kind: str = "html") -> None:
        session = load_session(str(body.get("session_id") or ""))
        if session is None:
            return self._json(400, {"error": "unknown session"})
        try:
            payload = export_pdf(session) if kind == "pdf" else export_html(session)
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except RuntimeError as exc:
            return self._json(500, {"error": str(exc)})
        if kind == "pdf":
            name, content_type = f"annotated-{session['dir'].name}.pdf", "application/pdf"
        else:
            name, content_type = f"annotated-{session['dir'].name}.html", "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    try:
        from llm_client import _load_api_key

        os.environ["OPENROUTER_API_KEY"] = _load_api_key()
        print("OpenRouter: API key loaded")
    except RuntimeError as exc:
        print(f"OpenRouter: {exc}")
    server = Server(("127.0.0.1", args.port), Handler)
    print(f"Claim checker UI: http://127.0.0.1:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
