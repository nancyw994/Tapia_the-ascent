#!/usr/bin/env python3
"""Local web UI for the step-by-step claim checker.

    python web/server.py            # http://127.0.0.1:8000

The page uploads a .md/.txt article or pastes a URL (fetched into article.md), then calls one step
at a time. Every step's result is saved to data/runs/<session_id>/.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

WEB = Path(__file__).resolve().parent
sys.path.insert(0, str(WEB.parent / "pipeline"))

import steps  # noqa: E402
from fetch_article import fetch_url_markdown  # noqa: E402
from lib import ROOT, write_json  # noqa: E402

ARTICLE_TYPES = {".md", ".txt"}
MAX_BODY = 600_000
MIN_CHARS, MAX_CHARS = 200, 200_000
SAMPLE = ROOT / "data" / "essays" / "shumer_2026-02-09.md"

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


class Server(ThreadingHTTPServer):
    allow_reuse_address = True


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
    data[key] = result
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
        else:
            self._json(404, {"error": f"not found: POST {path}. Restart the server if you just added Fetch link."})

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
        if essay_url:
            write_json(run_dir / "source.json", {"url": essay_url, "filename": filename})
        with lock:
            sessions[session_id] = {
                "article": text,
                "essay_url": essay_url,
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
        with lock:
            session = sessions.get(str(body.get("session_id")))
            if session is None or step not in STEP_INFO:
                return self._json(400, {"error": "unknown session or step"})
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
