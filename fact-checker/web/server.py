#!/usr/bin/env python3
"""Local web UI for the step-by-step claim checker.

    python web/server.py            # http://127.0.0.1:8000

Sign in (a demo account stored in data/users.json on this machine), then upload a .md
article or paste a URL (fetched into article.md) and run one step at a time. Each step streams the model's output to the page and
saves its result to data/runs/<run_id>/, which is also what the history sidebar lists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

WEB = Path(__file__).resolve().parent
sys.path.insert(0, str(WEB.parent / "pipeline"))

import steps  # noqa: E402
from fetch_article import fetch_url_markdown  # noqa: E402
from lib import ROOT, read_json, write_json  # noqa: E402

ARTICLE_TYPES = {".md", ".txt"}
MAX_BODY = 600_000
MIN_CHARS, MAX_CHARS = 200, 200_000
RUNS = ROOT / "data" / "runs"
USERS_FILE = ROOT / "data" / "users.json"
SAMPLE = ROOT / "data" / "essays" / "shumer_2026-02-09.md"
RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PBKDF2_ROUNDS = 200_000

# step name → (data key it produces, key it needs, file it is saved to)
STEP_INFO = {
    "extract": ("claims", None, "claims.json"),
    "classify": ("classifications", "claims", "classifications.json"),
    "sources": ("evidence", "classifications", "evidence.json"),
    "verdict": ("verdicts", "evidence", "verdicts.json"),
}
DATA_ORDER = [key for key, _, _ in STEP_INFO.values()]
FILE_FOR = {key: filename for key, _, filename in STEP_INFO.values()}

lock = threading.Lock()
sessions: dict[str, dict] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- accounts (a local demo store, not a real identity system) -----------------------


def load_users() -> dict:
    if USERS_FILE.is_file():
        try:
            return read_json(USERS_FILE)
        except json.JSONDecodeError:
            pass
    return {"users": {}, "tokens": {}}


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ROUNDS).hex()


def issue_token(store: dict, username: str) -> str:
    token = secrets.token_urlsafe(24)
    store["tokens"][token] = username
    write_json(USERS_FILE, store)
    return token


# --- runs ----------------------------------------------------------------------------


class Stream:
    """Events from the running step, read by the page with a cursor.

    Tokens that arrive between two polls are merged into one event, so a long reply costs
    a handful of events instead of thousands.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.seq = 0
        self.sent = 0
        self.lock = threading.Lock()

    def add(self, event: dict) -> None:
        with self.lock:
            last = self.events[-1] if self.events else None
            if (
                event.get("type") == "token"
                and last is not None
                and last["type"] == "token"
                and last["task"] == event["task"]
                and last.get("reasoning") == event.get("reasoning")
                and last["seq"] > self.sent  # not yet delivered, so it is safe to extend
            ):
                last["text"] += event["text"]
                return
            self.seq += 1
            self.events.append({**event, "seq": self.seq})
            if len(self.events) > 4000:
                del self.events[:1000]

    def since(self, cursor: int) -> tuple[list[dict], int]:
        with self.lock:
            self.sent = self.seq
            return [e for e in self.events if e["seq"] > cursor], self.seq

    def reset(self) -> None:
        with self.lock:
            self.events.clear()
            self.seq = self.sent = 0


def article_file(run_dir: Path) -> Path | None:
    return next((p for p in (run_dir / f"article{e}" for e in ARTICLE_TYPES) if p.is_file()), None)


def title_of(text: str, filename: str) -> str:
    for line in text.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()[:80] or filename
    return filename


def read_meta(run_dir: Path) -> dict | None:
    meta_file = run_dir / "meta.json"
    if not meta_file.is_file():
        return None
    try:
        return read_json(meta_file)
    except json.JSONDecodeError:
        return None


def run_data(run_dir: Path) -> dict:
    data = {}
    for key, _, filename in STEP_INFO.values():
        path = run_dir / filename
        if path.is_file():
            try:
                data[key] = read_json(path)[key]
            except (json.JSONDecodeError, KeyError):
                pass
    return data


def summarise(data: dict) -> dict:
    counts = {"claims": len(data.get("claims") or [])}
    for verdict in data.get("verdicts") or []:
        counts[verdict["verdict"]] = counts.get(verdict["verdict"], 0) + 1
    return counts


def save_meta(run_dir: Path, data: dict, **fields) -> dict:
    meta = read_meta(run_dir) or {}
    meta.update(fields)
    meta["counts"] = summarise(data)
    meta["steps_done"] = 1 + sum(1 for key in DATA_ORDER if key in data)
    meta["updated_at"] = now_iso()
    write_json(run_dir / "meta.json", meta)
    return meta


def run_step(session: dict, step: str) -> dict:
    key, needs, filename = STEP_INFO[step]
    data = session["data"]
    if needs and needs not in data:
        raise ValueError(f"run the earlier step first ({needs} is missing)")
    emit = session["stream"].add
    if step == "extract":
        result = steps.extract_claims(session["article"], emit=emit)
    elif step == "classify":
        result = steps.classify_claims(data["claims"], emit=emit)
    elif step == "sources":
        result = steps.find_sources(
            data["claims"], session["log"], emit=emit, essay_text=session["article"], essay_url=session.get("essay_url") or ""
        )
    else:
        result = steps.decide_verdicts(data["claims"], data["evidence"], emit=emit)
    for later in DATA_ORDER[DATA_ORDER.index(key) :]:
        data.pop(later, None)  # a re-run invalidates everything downstream
        (session["dir"] / FILE_FOR[later]).unlink(missing_ok=True)
    data[key] = result
    write_json(session["dir"] / filename, {key: result})
    save_meta(session["dir"], data)
    return {key: result}


class Handler(BaseHTTPRequestHandler):
    # --- plumbing --------------------------------------------------------------------

    def _send(self, code: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode())

    def _user(self) -> str | None:
        header = self.headers.get("Authorization") or ""
        token = header[7:] if header.startswith("Bearer ") else ""
        return load_users()["tokens"].get(token) if token else None

    def _session_for(self, run_id: str, user: str) -> dict | None:
        session = sessions.get(run_id)
        return session if session and session["user"] == user else None

    def log_message(self, fmt: str, *args) -> None:  # keep the terminal for pipeline output
        if "/api/progress" not in (args[0] if args else ""):
            super().log_message(fmt, *args)

    # --- routing ---------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path in {"/", "/index.html"}:
            return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/sample" and SAMPLE.is_file():
            return self._json(200, {"filename": SAMPLE.name, "text": SAMPLE.read_text(encoding="utf-8")})
        user = self._user()
        if user is None:
            return self._json(401, {"error": "please sign in"})
        if url.path == "/api/history":
            return self._history(user)
        if url.path == "/api/run":
            return self._open_run(user, (query.get("id") or [""])[0])
        if url.path == "/api/progress":
            return self._progress(user, (query.get("id") or [""])[0], (query.get("cursor") or ["0"])[0])
        self._json(404, {"error": "not found"})

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
        if path in {"/api/signup", "/api/signin"}:
            return self._auth(body, signup=path.endswith("signup"))
        user = self._user()
        if user is None:
            return self._json(401, {"error": "please sign in"})
        if path == "/api/signout":
            return self._signout()
        if path == "/api/upload":
            return self._upload(user, body)
        if path == "/api/from-url":
            return self._from_url(user, body)
        if path == "/api/step":
            return self._step(user, body)
        self._json(404, {"error": "not found"})

    # --- accounts --------------------------------------------------------------------

    def _auth(self, body: dict, signup: bool) -> None:
        username = str(body.get("username") or "").strip()[:64] or "guest"
        password = str(body.get("password") or "")
        # Demo mode: any username with any password signs in; unknown names are created on the fly.
        with lock:
            store = load_users()
            if username not in store["users"]:
                salt = secrets.token_hex(16)
                store["users"][username] = {"salt": salt, "hash": hash_password(password, salt), "created_at": now_iso()}
            self._json(200, {"username": username, "token": issue_token(store, username)})

    def _signout(self) -> None:
        header = self.headers.get("Authorization") or ""
        with lock:
            store = load_users()
            store["tokens"].pop(header[7:], None)
            write_json(USERS_FILE, store)
        self._json(200, {"ok": True})

    # --- history ---------------------------------------------------------------------

    def _history(self, user: str) -> None:
        mine, examples = [], []
        for run_dir in RUNS.iterdir() if RUNS.is_dir() else []:
            meta = read_meta(run_dir) if run_dir.is_dir() else None
            if not meta:
                continue  # runs from the command-line pipeline have no meta.json
            if meta.get("example"):
                examples.append(meta)
            elif meta.get("user") == user:
                mine.append(meta)
        by_time = lambda m: m.get("updated_at") or ""  # noqa: E731
        self._json(200, {"runs": sorted(mine, key=by_time, reverse=True)[:30], "examples": sorted(examples, key=by_time)})

    def _open_run(self, user: str, run_id: str) -> None:
        if not RUN_ID.match(run_id):
            return self._json(400, {"error": "bad run id"})
        run_dir = RUNS / run_id
        meta = read_meta(run_dir) if run_dir.is_dir() else None
        source = article_file(run_dir) if meta else None
        if not meta or not source:
            return self._json(404, {"error": "no such run"})
        if not meta.get("example") and meta.get("user") != user:
            return self._json(403, {"error": "that run belongs to another account"})
        article, data = source.read_text(encoding="utf-8"), run_data(run_dir)
        if not meta.get("example"):  # let the owner carry on from where they stopped
            with lock:
                sessions.setdefault(
                    run_id,
                    {
                        "article": article,
                        "data": data,
                        "dir": run_dir,
                        "log": ROOT / "notes" / f"sources_{run_id}.log",
                        "user": user,
                        "essay_url": meta.get("source_url") or "",
                        "running": False,
                        "stream": Stream(),
                    },
                )["data"] = data
        self._json(200, {"meta": meta, "article": article, "data": data, "read_only": bool(meta.get("example"))})

    # --- running ---------------------------------------------------------------------

    def _upload(self, user: str, body: dict) -> None:
        filename, text = str(body.get("filename") or ""), body.get("text")
        suffix = Path(filename).suffix.lower()
        if suffix not in ARTICLE_TYPES:
            return self._json(400, {"error": "please upload a .md or .txt file"})
        self._start_run(user, filename, text)

    def _from_url(self, user: str, body: dict) -> None:
        url = str(body.get("url") or "").strip()
        try:
            text, canonical = fetch_url_markdown(url)
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            return self._json(502, {"error": f"could not fetch that link: {type(exc).__name__}: {exc}"})
        self._start_run(user, "article.md", f"# Source\n\n{canonical}\n\n{text}\n", essay_url=canonical)

    def _start_run(self, user: str, filename: str, text, essay_url: str = "") -> None:
        suffix = Path(filename).suffix.lower()
        if not isinstance(text, str) or len(text.strip()) < MIN_CHARS:
            return self._json(400, {"error": f"the article is too short (need at least {MIN_CHARS} characters)"})
        if len(text) > MAX_CHARS:
            return self._json(413, {"error": f"the article is too long (limit {MAX_CHARS:,} characters)"})
        run_id = f"{datetime.now():%Y%m%dT%H%M%S}-{secrets.token_hex(2)}"
        run_dir = RUNS / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / f"article{suffix}").write_text(text, encoding="utf-8")
        meta = save_meta(
            run_dir,
            {},
            run_id=run_id,
            user=user,
            filename=Path(filename).name,
            title=title_of(text, Path(filename).name),
            words=len(text.split()),
            created_at=now_iso(),
            example=False,
            source_url=essay_url,
        )
        with lock:
            sessions[run_id] = {
                "article": text,
                "data": {},
                "dir": run_dir,
                "log": ROOT / "notes" / f"sources_{run_id}.log",
                "user": user,
                "essay_url": essay_url,
                "running": False,
                "stream": Stream(),
            }
        self._json(200, {"run_id": run_id, "meta": meta, "text": text})  # a fetched link's text is new to the page

    def _progress(self, user: str, run_id: str, cursor: str) -> None:
        session = self._session_for(run_id, user)
        if session is None:
            return self._json(404, {"error": "unknown run"})
        events, seq = session["stream"].since(int(cursor) if cursor.isdigit() else 0)
        self._json(200, {"events": events, "cursor": seq})

    def _step(self, user: str, body: dict) -> None:
        step = body.get("step")
        with lock:
            session = self._session_for(str(body.get("run_id")), user)
            if session is None or step not in STEP_INFO:
                return self._json(400, {"error": "unknown run or step"})
            if session["running"]:
                return self._json(409, {"error": "a step is already running for this article"})
            session["running"] = True
            session["stream"].reset()
        try:
            self._json(200, run_step(session, step))
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            session["running"] = False


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Claim checker UI: http://127.0.0.1:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
