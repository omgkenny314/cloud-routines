#!/usr/bin/env python3
"""Double-O HQ — accessible web chat for Kenny's agents.

The VoiceOver-first front door to the same brains that answer on Telegram.
Serves a small web app over the LAN; replies run through bot_runner's
load_persona/run_claude and the same history + journal files, so P.A. on
the web and P.A. on Telegram are one continuous conversation.

Stdlib only, like the rest of the repo. Run detached with a PID file:

    nohup /usr/bin/python3 webchat/server.py \
        >> ~/.local/state/double-o-agents/logs/webchat.log 2>&1 &

Auth: every /api call needs the key from ~/.config/double-o-agents/webchat.key
(generated on first run). The UI stores it in localStorage after Kenny opens
the keyed link once. LAN-only by design — no tunnel, no port forward.
"""

import json
import re
import secrets
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot_runner as br  # noqa: E402 — path shim must run first
import push_notify as pn  # noqa: E402 — subscription storage + VAPID key

PORT = 8377
STATIC = Path(__file__).resolve().parent / "static"
KEY_FILE = br.CONFIG_DIR / "webchat.key"
PID_FILE = br.STATE_DIR / "webchat.pid"

# Health Auto Export relay (added 2026-08-08) — third leg of the health-data
# pipeline greenlit 2026-08-02. The iPhone app POSTs here (its own API-key
# header, separate secret from webchat's own key) whenever it runs its
# automation; this stores the metrics for health_tools/health_export_read.py
# to serve to Hollis + Nat. Catches weight/body-comp from the FitIndex scale
# (its only automated door — no vendor API exists) and doubles as a backup
# lane for glucose/BP if Kenny ever turns those categories on too.
HEALTH_EXPORT_KEY_FILE = br.CONFIG_DIR / "health-export.key"
HEALTH_EXPORT_STORE = br.STATE_DIR / "health-export" / "metrics.json"

# Thread ids double as history keys — "duo-health" is the same key the
# Telegram huddle writes, so the huddle's context stays whole across doors.
# "group" decides which collapsible section the thread lives in on the main
# page — "Personal" plus one section per business ("OMG Kenny", "Vibe
# Worldwide", ...). Threads without a group land in Personal.
THREADS = {
    "pa": {"kind": "single", "agent": "pa", "display": "P.A.",
           "tagline": "Personal assistant — tasks, calendar, research",
           "group": "Personal"},
    "marcus": {"kind": "single", "agent": "marcus", "display": "Marcus",
               "tagline": "Mindset and psychology coach",
               "group": "Personal"},
    "duo-health": {"kind": "duo", "duo": "health", "display": "Hollis + Nat",
                   "tagline": "The health duo — both reply",
                   "group": "Personal"},
    "finn": {"kind": "single", "agent": "finn", "display": "Finn",
             "tagline": "Financial agent — bills, budget, stocks, wealth",
             "group": "Personal"},
    "drip": {"kind": "single", "agent": "drip", "display": "Drip",
             "tagline": "Personal stylist — fits, trends, and shopping",
             "group": "Personal"},
    "arc": {"kind": "single", "agent": "arc", "display": "ARC",
            "tagline": "Archive — videos, articles, ideas held for later",
            "group": "Personal"},
}

MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
        ".css": "text/css", ".png": "image/png",
        ".webmanifest": "application/manifest+json"}

# Media lane (added 2026-08-09): clip previews + photo uploads.
# /api/media serves files ONLY from these roots — the clip libraries ARC cuts
# into, and the HQ uploads folder. Anything else 403s, symlinks resolved
# first. Uploads land outside the repo, like all state.
UPLOADS_DIR = br.STATE_DIR / "uploads"
MEDIA_ROOTS = (
    (Path.home() / "Documents/MyLifeOS/MyCodeOS/video-editor/assets/clips").resolve(),
    (Path.home() / "Documents/MyLifeOS/MyKnowledgeOS/video-library/clips").resolve(),
    UPLOADS_DIR.resolve(),
)
UPLOAD_MAX_BYTES = 25 * 1024 * 1024
MEDIA_MIME = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
              ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
              ".gif": "image/gif", ".webp": "image/webp", ".heic": "image/heic"}

pending = {}  # thread id -> True while a reply is being generated
pending_lock = threading.Lock()
# thread id -> True if messages arrived while a reply was cooking. The old
# behavior 409-rejected those sends and the UI quietly dropped them — to a
# VoiceOver user mid-flow that read as "the agent stopped responding"
# (2026-08-09, second Drip "outage" in two days). Now every send is saved to
# history up front; the worker loops until no unanswered user messages remain.
queued_more = {}

# thread id -> last time the app polled it. While Kenny has a thread open the
# app polls every few seconds; if the last poll is stale when a reply lands,
# he's wandered off — push the reply to his phone (Kenny's ask, 2026-08-02).
last_seen = {}
WATCHING_SECS = 20


def push_reply_if_away(tid: str, display: str, text: str) -> None:
    if time.time() - last_seen.get(tid, 0) < WATCHING_SECS:
        return  # he's watching the thread right now
    try:
        pn.send(display, text[:400], thread=tid)
    except Exception as e:  # noqa: BLE001 — push must never sink the reply
        br.log("webchat", f"reply push failed: {e}")


def store_health_export(payload: dict) -> int:
    """Merge one Health Auto Export POST into the rolling per-metric store,
    deduping by (metric name, date) so a re-sent batch doesn't double up.
    Returns the number of new data points actually stored."""
    metrics = ((payload.get("data") or {}).get("metrics")) or []
    HEALTH_EXPORT_STORE.parent.mkdir(parents=True, exist_ok=True)
    store = {}
    if HEALTH_EXPORT_STORE.exists():
        try:
            store = json.loads(HEALTH_EXPORT_STORE.read_text())
        except json.JSONDecodeError:
            store = {}
    added = 0
    for metric in metrics:
        name = metric.get("name")
        points = metric.get("data") or []
        if not name or not points:
            continue
        entry = store.setdefault(name, {"units": metric.get("units"), "data": []})
        entry["units"] = metric.get("units", entry.get("units"))
        seen = {p.get("date") for p in entry["data"]}
        for p in points:
            if p.get("date") not in seen:
                entry["data"].append(p)
                seen.add(p.get("date"))
                added += 1
        entry["data"].sort(key=lambda p: p.get("date") or "")
    HEALTH_EXPORT_STORE.write_text(json.dumps(store))
    return added


def load_key() -> str:
    if not KEY_FILE.exists():
        KEY_FILE.write_text(secrets.token_urlsafe(16))
        KEY_FILE.chmod(0o600)
    return KEY_FILE.read_text().strip()


API_KEY = load_key()


PHOTO_NOTE = (
    "\n\nKenny attached one or more photos — the [Attached photo: ...] paths "
    "in his message. Use the Read tool on each exact path to actually look at "
    "the photo before answering. If reading a photo fails, say so honestly "
    "instead of guessing at what it shows."
)


def _photo_note(text: str) -> str:
    return PHOTO_NOTE if "[Attached photo:" in text else ""


def _trailing_user_block(history: list) -> tuple[list, str]:
    """Split history into (everything before, joined text of) the unanswered
    user messages at the tail. More than one lands there when Kenny sends
    again while a reply is cooking — all of them get answered as one turn."""
    i = len(history)
    while i > 0 and history[i - 1]["role"] == "user":
        i -= 1
    return history[:i], "\n\n".join(m["text"] for m in history[i:])


def single_worker(thread_id: str, agent: str, text: str) -> None:
    """Mirror of bot_runner.ask_claude. The user's message is already in
    history (do_POST saves it before spawning us, so the UI renders it while
    the reply cooks). Loops while queued_more says new messages arrived
    mid-reply. On failure the apology is appended too — a silent thread
    reads as broken."""
    cfg = br.AGENTS[agent]
    while True:
        history = br.load_history(agent)
        prior, new_text = _trailing_user_block(history)
        if not new_text:  # queued flag raced a message that's already answered
            with pending_lock:
                pending.pop(thread_id, None)
            return
        convo = "\n\n".join(
            f"{'Kenny' if m['role'] == 'user' else cfg['display']}: {m['text']}"
            for m in prior
        )
        now = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
        prompt = (
            f"Current date and time: {now}.\n\n"
            f"Recent conversation:\n{convo if convo else '(this is the first conversation)'}\n\n"
            f"Kenny's new message:\n{new_text}{_photo_note(new_text)}\n\n"
            f"Reply as {cfg['display']} — plain text only, no markdown symbols."
        )
        system = br.load_persona(cfg) + (
            "\n\nYour final output is sent verbatim to Kenny as a chat message. "
            "Output ONLY the reply text — no preamble, no metadata."
        )
        try:
            reply = br.run_claude(cfg, system, prompt)
            br.journal_log(cfg, new_text, reply)
        except Exception as e:  # noqa: BLE001 — any brain failure gets the apology
            br.log(agent, f"webchat claude error {e}")
            reply = ("I hit a snag thinking that one through. "
                     "Give me another try in a minute.")
        history = br.load_history(agent)
        history.append({"role": "assistant", "text": reply, "ts": time.time()})
        br.save_history(agent, history)
        push_reply_if_away(thread_id, cfg["display"], reply)
        br.log(agent, f"webchat replied ({len(reply)} chars)")
        with pending_lock:
            if queued_more.pop(thread_id, None):
                continue  # messages arrived while we were replying — go again
            pending.pop(thread_id, None)
            return


def duo_worker(thread_id: str, duo: str, text: str) -> None:
    """The huddle over the web: same shared history the Telegram group used,
    each member replies in order and sees the counterpart's fresh reply.
    Like single_worker, the user's message is already in history and the
    loop drains anything that arrived while the huddle was talking."""
    key = f"duo-{duo}"
    while True:
        history = br.load_history(key)
        _, new_text = _trailing_user_block(history)
        if not new_text:
            with pending_lock:
                pending.pop(thread_id, None)
            return
        replies = []
        for member in br.DUOS[duo]["members"]:
            cfg = br.AGENTS[member]
            history = br.load_history(key)
            now = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
            prompt = (
                f"Current date and time: {now}.\n\n"
                f"The group chat so far:\n"
                f"{br.render_duo_convo(history) or '(this is the first conversation)'}"
                f"{_photo_note(new_text)}\n\n"
                f"Reply as {cfg['display']} — plain text only, no markdown symbols."
            )
            try:
                reply = br.run_claude(cfg, br.load_persona(cfg) + br.DUO_NOTE, prompt)
            except Exception as e:  # noqa: BLE001
                br.log(member, f"webchat claude error {e}")
                reply = (f"{cfg['display']} here — I hit a snag thinking that one "
                         "through. Give me another try in a minute.")
            history.append({"role": "assistant", "speaker": cfg["display"],
                            "text": reply, "ts": time.time()})
            br.save_history(key, history)
            replies.append(reply)
            br.log(member, f"webchat duo reply ({len(reply)} chars)")
        members = br.DUOS[duo]["members"]
        br.journal_log(br.AGENTS[members[0]], new_text, replies[0])
        for m, r in zip(members[1:], replies[1:]):
            br.journal_log(br.AGENTS[m], None, r, kind="group reply")
        if replies:
            push_reply_if_away(thread_id, " + ".join(
                br.AGENTS[m]["display"] for m in members), replies[-1])
        with pending_lock:
            if queued_more.pop(thread_id, None):
                continue
            pending.pop(thread_id, None)
            return


def thread_messages(thread_id: str) -> list:
    cfg = THREADS[thread_id]
    history = br.load_history(
        cfg["agent"] if cfg["kind"] == "single" else thread_id)
    out = []
    for m in history:
        speaker = ("Kenny" if m["role"] == "user"
                   else m.get("speaker") or cfg["display"])
        out.append({"speaker": speaker, "mine": m["role"] == "user",
                    "text": m["text"], "ts": m.get("ts")})
    return out


class Handler(BaseHTTPRequestHandler):

    # HTTP/1.1 is load-bearing for <video>: browsers' media stacks won't
    # stream from an HTTP/1.0 server (readyState stays 0, no error — caught
    # live in Chrome, 2026-08-09). Safe here because every response path
    # sets Content-Length.
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _serve_media(self, target: Path) -> None:
        """Stream one whitelisted media file, honoring single-range requests —
        iOS Safari refuses to play <video> from a server without Range
        support, so this is load-bearing for clip previews on Kenny's phone."""
        size = target.stat().st_size
        ctype = MEDIA_MIME.get(target.suffix.lower(), "application/octet-stream")
        start, end = 0, size - 1
        rng = self.headers.get("Range", "")
        partial = False
        if rng.startswith("bytes="):
            spec = rng.split("=", 1)[1].split(",")[0].strip()
            s, _, e = spec.partition("-")
            try:
                if s:
                    start = int(s)
                    if e:
                        end = min(int(e), size - 1)
                elif e:  # suffix form: last N bytes
                    start = max(0, size - int(e))
                partial = True
            except ValueError:
                start, end, partial = 0, size - 1, False
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
        self.send_response(206 if partial else 200)
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        with open(target, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _authed(self) -> bool:
        supplied = self.headers.get("X-Auth-Key", "")
        if not supplied:
            qs = parse_qs(urlparse(self.path).query)
            supplied = (qs.get("key") or [""])[0]
        return secrets.compare_digest(supplied, API_KEY)

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler contract
        path = urlparse(self.path).path
        if path == "/oauth/ihealth/callback":
            qs = parse_qs(urlparse(self.path).query)
            code = (qs.get("code") or [""])[0]
            state = (qs.get("state") or [""])[0]
            error = (qs.get("error") or [""])[0]
            out = br.STATE_DIR / "ihealth-oauth-code.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            if error:
                out.write_text(json.dumps({"error": error,
                                            "description": (qs.get("error_description") or [""])[0]}))
                body = b"<h1>Sign-in failed or was cancelled. You can close this tab.</h1>"
            elif code:
                out.write_text(json.dumps({"code": code, "state": state,
                                            "received_at": time.time()}))
                body = b"<h1>Signed in. You can close this tab.</h1>"
            else:
                body = b"<h1>No code received. You can close this tab.</h1>"
            return self._send(200, body, "text/html")
        if path.startswith("/api/"):
            if not self._authed():
                return self._json(401, {"error": "bad key"})
            if path == "/api/threads":
                items = []
                for tid, cfg in THREADS.items():
                    msgs = thread_messages(tid)
                    items.append({
                        "id": tid, "display": cfg["display"],
                        "tagline": cfg["tagline"],
                        "group": cfg.get("group", "Personal"),
                        "last": msgs[-1] if msgs else None,
                    })
                return self._json(200, {"threads": items})
            if path == "/api/thread":
                qs = parse_qs(urlparse(self.path).query)
                tid = (qs.get("id") or [""])[0]
                if tid not in THREADS:
                    return self._json(404, {"error": "no such thread"})
                with pending_lock:
                    busy = bool(pending.get(tid))
                last_seen[tid] = time.time()
                return self._json(200, {"messages": thread_messages(tid),
                                        "pending": busy})
            if path == "/api/push/vapid-key":
                if not pn.PUB_KEY.exists():
                    return self._json(503, {"error": "push not initialized"})
                return self._json(200, {"key": pn.public_key()})
            if path == "/api/media":
                qs = parse_qs(urlparse(self.path).query)
                raw = (qs.get("path") or [""])[0]
                try:
                    target = Path(raw).resolve()
                except (OSError, ValueError):
                    return self._json(400, {"error": "bad path"})
                if not any(root == target or root in target.parents
                           for root in MEDIA_ROOTS):
                    return self._json(403, {"error": "path outside media roots"})
                if not target.is_file():
                    return self._json(404, {"error": "no such file"})
                br.log("webchat", f"media request: {target.name} "
                                  f"range={self.headers.get('Range', '(none)')} "
                                  f"ua={self.headers.get('User-Agent', '')[:40]}")
                try:
                    return self._serve_media(target)
                except (BrokenPipeError, ConnectionResetError):
                    return  # player closed mid-stream — normal, not an error
            return self._json(404, {"error": "unknown endpoint"})

        name = "index.html" if path == "/" else path.lstrip("/")
        target = (STATIC / name).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = MIME.get(target.suffix, "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health-export/receive":
            supplied = self.headers.get("X-API-Key", "")
            expected = (HEALTH_EXPORT_KEY_FILE.read_text().strip()
                        if HEALTH_EXPORT_KEY_FILE.exists() else "")
            if not expected or not secrets.compare_digest(supplied, expected):
                return self._json(401, {"error": "bad key"})
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length).decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._json(400, {"error": "bad json"})
            try:
                n = store_health_export(body)
            except Exception as e:  # noqa: BLE001 — a bad payload must not 500 the app
                br.log("webchat", f"health-export store failed: {e}")
                return self._json(400, {"error": "could not store payload"})
            br.log("webchat", f"health-export received ({n} metric point(s))")
            return self._json(200, {"ok": True, "points_stored": n})
        if path == "/api/upload":
            # Photo upload (added 2026-08-09): raw bytes in, saved path out.
            # The client then folds "[Attached photo: <path>]" into the chat
            # message; agents Read the file (their allowlists grant the
            # uploads folder read-only).
            if not self._authed():
                return self._json(401, {"error": "bad key"})
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return self._json(400, {"error": "empty upload"})
            if length > UPLOAD_MAX_BYTES:
                return self._json(413, {"error": "photo too large (25 MB cap)"})
            raw_name = self.headers.get("X-Filename", "photo.jpg")
            safe = re.sub(r"[^A-Za-z0-9._-]", "-", raw_name).strip("-.")[-80:] or "photo.jpg"
            sub = UPLOADS_DIR / datetime.now().strftime("%Y-%m")
            sub.mkdir(parents=True, exist_ok=True)
            dest = sub / f"{int(time.time() * 1000)}-{safe}"
            dest.write_bytes(self.rfile.read(length))
            br.log("webchat", f"photo uploaded: {dest.name} ({length} bytes)")
            return self._json(200, {"path": str(dest)})
        if path not in ("/api/send", "/api/push/subscribe"):
            return self._json(404, {"error": "unknown endpoint"})
        if not self._authed():
            return self._json(401, {"error": "bad key"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length).decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._json(400, {"error": "bad json"})
        if path == "/api/push/subscribe":
            try:
                pn.add_subscription(body)
            except ValueError:
                return self._json(400, {"error": "not a subscription"})
            br.log("webchat", "push subscription stored")
            return self._json(200, {"ok": True})
        tid = body.get("thread")
        text = (body.get("text") or "").strip()
        if tid not in THREADS or not text:
            return self._json(400, {"error": "need thread + text"})
        cfg = THREADS[tid]
        # Save the message to history FIRST, busy or not — it must never
        # vanish. If a reply is cooking, flag the worker to go again when it
        # finishes instead of 409-rejecting the send (2026-08-09 fix).
        key = cfg["agent"] if cfg["kind"] == "single" else tid
        history = br.load_history(key)
        history.append({"role": "user", "text": text, "ts": time.time()})
        br.save_history(key, history)
        with pending_lock:
            if pending.get(tid):
                queued_more[tid] = True
                return self._json(202, {"ok": True, "queued": True})
            pending[tid] = True
        if cfg["kind"] == "single":
            worker = threading.Thread(
                target=single_worker, args=(tid, cfg["agent"], text), daemon=True)
        else:
            worker = threading.Thread(
                target=duo_worker, args=(tid, cfg["duo"], text), daemon=True)
        worker.start()
        return self._json(202, {"ok": True})

    def log_message(self, fmt, *args):  # quiet the per-request stderr spam
        pass


def main() -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(__import__("os").getpid()))
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    br.log("webchat", f"Double-O HQ listening on port {PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
