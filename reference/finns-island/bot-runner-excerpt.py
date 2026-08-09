#!/usr/bin/env python3
"""Double-O Agents — bot runner.

Runs Kenny's personal agents. Brains: Claude Code CLI in headless mode (uses
Kenny's subscription, no API key).

Transport: Double-O HQ (webchat/server.py) only, as of 2026-08-08 — Telegram
is retired (not accessible enough with VoiceOver; Kenny's own web app works
better). The Telegram plumbing below (pollers, send_message, token_file)
still exists but every agent's token_file is now absent on purpose, so
main() never starts a poller and deliver_unprompted() falls through to HQ
push only — same "no token = HQ-only" pattern Finn/Drip/ARC proved first.
Kept rather than ripped out: cheap to leave dormant, and it's the working
reference if a real phone-number transport (Twilio/iMessage) ever comes back.

Stdlib only — no pip installs. Python 3.9+.
"""

import json
import os
import random
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

HOME = Path.home()
CONFIG_DIR = HOME / ".config" / "double-o-agents"
STATE_DIR = HOME / ".local" / "state" / "double-o-agents"
REPO_DIR = Path(__file__).resolve().parent
CLAUDE_BIN = HOME / ".local" / "bin" / "claude"
VAULT_MARCUS = (
    HOME / "Documents" / "MyLifeOS" / "MyKnowledgeOS" / "THE MIND OF KENNY D." / "marcus"
)
# The health duo's shared journal — sensitive layer, stays local, never staged.
VAULT_HEALTH = HOME / "Documents" / "MyLifeOS" / "MyHealthOS" / "hollis-nat"
# Finn's journal — financial layer, stays local, never staged.
VAULT_FINN = HOME / "Documents" / "MyLifeOS" / "MyFinancialOS" / "finn"
# Drip's room — sizes, brands, closet, trend log, journal.
VAULT_DRIP = HOME / "Documents" / "MyLifeOS" / "MeOS" / "drip"

ALLOWED_USER_ID = int((CONFIG_DIR / "allowed_user_id").read_text().strip())

HISTORY_LIMIT = 40  # messages kept per agent for conversational memory
CLAUDE_TIMEOUT = 240  # seconds
POLL_TIMEOUT = 50  # Telegram long-poll seconds

# Baseline: no side-effect tools. Agents opt IN to specific capabilities via
# "allowed_tools" — an explicit allowlist of permission rules; everything else
# stays denied in -p mode.
DISALLOWED_NO_BASH = (
    "Edit,Write,NotebookEdit,WebFetch,WebSearch,Agent,TodoWrite,KillShell,BashOutput"
)
DISALLOWED_ALL = "Bash," + DISALLOWED_NO_BASH

# P.A. keeps the same baseline minus the web tools — he researches on Kenny's
# behalf (granted 2026-07-26). Web stays denied for Marcus.
DISALLOWED_PA = (
    "Edit,Write,NotebookEdit,Agent,TodoWrite,KillShell,BashOutput"
)

PA_TODOIST = f"Bash(python3 {REPO_DIR}/pa_tools/todoist.py:*)"
# Link wallet hands (granted 2026-07-31). The wrapper enforces a per-request
# dollar ceiling and never exposes card credentials; every request still needs
# Kenny's approval in the Link app before a credential exists. Raw link-cli is
# deliberately NOT allowlisted — the wrapper is the boundary.
PA_WALLET = f"Bash(python3 {REPO_DIR}/pa_tools/wallet.py:*)"
# Memory pen (granted 2026-07-31): P.A. saves durable facts about Kenny to a
# private file outside the repo; the whole file rides in his system prompt.
PA_MEMORY = f"Bash(python3 {REPO_DIR}/pa_tools/memory.py:*)"
# Calendar hands (granted 2026-07-31): both Gmail calendars, full CRUD. Delete
# is two-stage in the toolkit itself — it refuses without flags P.A. may only
# pass after Kenny types the word "Delete" alone in Telegram (personality
# file), with an extra flag when the event has other guests. gcal_auth.py
# (the sign-in helper) is deliberately NOT allowlisted.
PA_GCAL = f"Bash(python3 {REPO_DIR}/pa_tools/gcal.py:*)"
# Journal hands (granted 2026-08-01): files/refiles/recalls Kenny's voice-memo
# journal entries. Append-only by construction — journal.py has no delete or
# edit-body command, and enrich refuses to touch anything but frontmatter.
PA_JOURNAL = f"Bash(python3 {REPO_DIR}/pa_tools/journal.py:*)"
# Email hands (granted 2026-08-09): both Gmail inboxes via gmail.py. The
# toolkit has NO delete, trash, or send subcommand — inbox-clearing goes only
# through `bench` (the "P.A. Bench" label: out of the inbox, never gone), and
# the gmail.modify scope can't permanently delete anyway. gmail_auth.py (the
# sign-in helper) is deliberately NOT allowlisted.
PA_GMAIL = f"Bash(python3 {REPO_DIR}/pa_tools/gmail.py:*)"
# File hands (granted 2026-08-09, Kenny's explicit full-access call: P.A. is
# the catch-all assistant, so no room in MyLifeOS is off-limits to him —
# including the sensitive layers). Read/Edit/Write via permission rules;
# move/copy/mkdir/trash via pa_tools/files.py (fenced to MyLifeOS, no delete
# subcommand exists — "trash" goes to the recoverable boneyard). P.A.'s
# disallowed list drops Edit,Write accordingly (now same as Finn's).
PA_FILES = f"Bash(python3 {REPO_DIR}/pa_tools/files.py:*)"
PA_MYLIFEOS = [
    f"Read(/{HOME}/Documents/MyLifeOS/**)",
    f"Edit(/{HOME}/Documents/MyLifeOS/**)",
    f"Write(/{HOME}/Documents/MyLifeOS/**)",
]
PA_ALLOWED = ",".join([PA_TODOIST, PA_WALLET, PA_MEMORY, PA_GCAL, PA_JOURNAL,
                       PA_GMAIL, PA_FILES, *PA_MYLIFEOS,
                       "WebSearch", "WebFetch"])

# Hollis + Nat (the health duo) — look-only hands: web research, read access to
# MyHealthOS and the video library, read-only Todoist, video transcripts.
# Deliberately absent: anything that writes, sends, or changes state.
HEALTH_ALLOWED = ",".join([
    f"Bash(python3 {REPO_DIR}/health_tools/todoist_read.py:*)",
    f"Bash(python3 {REPO_DIR}/health_tools/video_transcript.py:*)",
    # Calendar eyes (granted 2026-08-02): read-only view of both Gmail
    # calendars so the duo can see doctor's appointments and follow up on
    # results. The tool has no write subcommands; gcal_auth.py stays off.
    f"Bash(python3 {REPO_DIR}/health_tools/gcal_read.py:*)",
    # Blood pressure + pulse eyes (granted 2026-08-08, first step of the
    # health-data pipeline greenlit 2026-08-02). Read-only; ihealth_auth.py
    # (the one-time OAuth sign-in) stays off, same pattern as gcal_auth.py.
    f"Bash(python3 {REPO_DIR}/health_tools/ihealth.py:*)",
    # Health Auto Export relay eyes (granted 2026-08-08, third step of the
    # pipeline) — weight/body-comp from the FitIndex scale, the only
    # automated door it has. Read-only; the iPhone app posts to webchat's
    # /health-export/receive route on its own schedule, this tool just reads
    # what already landed.
    f"Bash(python3 {REPO_DIR}/health_tools/health_export_read.py:*)",
    f"Read(/{HOME}/Documents/MyLifeOS/MyHealthOS/**)",
    f"Read(/{HOME}/Documents/MyLifeOS/MyKnowledgeOS/video-library/**)",
    "WebSearch",
    "WebFetch",
])

# Finn (the financial agent, 2026-08-02) — first HQ-only agent: no Telegram
# token exists, so main() never starts him a poller; he answers through the
# webchat. Read-only on real money by design. Hands: web research, read/write
# on MyFinancialOS (bills, budget, his watch list), and Todoist guarded by the
# toolkit itself to touch only tasks Finn created.
DISALLOWED_FINN = "NotebookEdit,Agent,TodoWrite,KillShell,BashOutput"
FINN_ALLOWED = ",".join([
    f"Bash(python3 {REPO_DIR}/finn_tools/todoist.py:*)",
    f"Read(/{HOME}/Documents/MyLifeOS/MyFinancialOS/**)",
    f"Edit(/{HOME}/Documents/MyLifeOS/MyFinancialOS/**)",
    "WebSearch",
    "WebFetch",
])

# Drip (the stylist, 2026-08-07) — HQ-only like Finn. Hands: web scouting,
# the Link wallet (same guarded plumbing as P.A.'s, own audit log — Kenny's
# v0 call: checkout IS the accessibility win Drip exists for), Todoist guarded
# by ownership ledger, read-only calendar eyes (reuses health_tools/gcal_read),
# and read/write scoped to his room in MeOS. Tape-measure photo sizing is a
# phase-2 build (HQ has no image path yet).
DRIP_ALLOWED = ",".join([
    f"Bash(python3 {REPO_DIR}/drip_tools/todoist.py:*)",
    f"Bash(python3 {REPO_DIR}/drip_tools/wallet.py:*)",
    f"Bash(python3 {REPO_DIR}/health_tools/gcal_read.py:*)",
    f"Read(/{HOME}/Documents/MyLifeOS/MeOS/drip/**)",
    f"Edit(/{HOME}/Documents/MyLifeOS/MeOS/drip/**)",
    "WebSearch",
    "WebFetch",
])

# ARC (Archive, 2026-08-07) — HQ-only like Finn and Drip. Kenny's front office
# for raw sparks: video links route through the video-notes pipeline into
# video-library/, articles and social posts get fetched, summarized, and
# captured (with Kenny's own "why did this catch my eye") into idea-archive/.
# Deliberately absent: any tool that would let ARC draft the actual piece for
# him — outline-building only, by Kenny's explicit call (2026-08-07).
#
# Video transcription is ASYNC (rebuilt 2026-08-07 after a live test proved
# the synchronous version times out on anything but a short clip — measured
# 18 real minutes for a 28-minute video, local WhisperX with no GPU). ARC
# only ever gets `detect` (fast, routing) and arc_tools/video_job.py (fast,
# kicks the slow work off detached). Direct access to video_notes.py's
# `process` subcommand is deliberately NOT on ARC's allowlist — that's the
# one thing that blocked a whole reply for 18+ minutes.
# Clipper power (granted 2026-08-09): ARC can search the clip library and
# locate quotes in existing transcripts synchronously (fast, text-only), but
# cutting a NEW clip goes through arc_tools/clip_job.py — clipper.py's `pull`
# downloads whole sources and may WhisperX them, the same unbounded-runtime
# shape that forced video async. `pull` and `verify` (also WhisperX) are
# deliberately NOT allowlisted.
ARC_ALLOWED = ",".join([
    f"Bash(python3 {HOME}/Documents/MyLifeOS/MyCodeOS/video-notes/video_notes.py detect:*)",
    f"Bash(python3 {REPO_DIR}/arc_tools/video_job.py:*)",
    f"Bash(python3 {REPO_DIR}/arc_tools/clip_job.py:*)",
    f"Bash(python3 {HOME}/Documents/MyLifeOS/MyCodeOS/video-editor/.claude/skills/clipper/clipper.py catalog:*)",
    f"Bash(python3 {HOME}/Documents/MyLifeOS/MyCodeOS/video-editor/.claude/skills/clipper/clipper.py find:*)",
    f"Read(/{HOME}/Documents/MyLifeOS/MyCodeOS/video-editor/assets/clips/**)",
    f"Read(/{HOME}/Documents/MyLifeOS/MyCodeOS/video-editor/transcripts/**)",
    f"Read(/{HOME}/Documents/MyLifeOS/MyKnowledgeOS/video-library/**)",
    f"Edit(/{HOME}/Documents/MyLifeOS/MyKnowledgeOS/video-library/**)",
    f"Read(/{HOME}/Documents/MyLifeOS/MyKnowledgeOS/idea-archive/**)",
    f"Edit(/{HOME}/Documents/MyLifeOS/MyKnowledgeOS/idea-archive/**)",
    "WebSearch",
    "WebFetch",
])

AGENTS = {
    "pa": {
        "display": "P.A.",
        "token_file": CONFIG_DIR / "telegram-pa.token",  # absent on purpose — Telegram retired 2026-08-08, HQ-only
        "personality": REPO_DIR / "agents" / "pa.md",
        "journal": STATE_DIR / "journal" / "pa",
        "memory": STATE_DIR / "memory" / "pa.md",
        "allowed_tools": PA_ALLOWED,
        # Edit/Write un-disallowed 2026-08-09 for the file hands — same
        # baseline Finn has had since 2026-08-02.
        "disallowed_tools": DISALLOWED_FINN,
    },
    "marcus": {
        "display": "Marcus",
        "token_file": CONFIG_DIR / "telegram-marcus.token",  # absent on purpose — Telegram retired 2026-08-08, HQ-only
        "personality": REPO_DIR / "agents" / "marcus.md",
        "journal": VAULT_MARCUS,
        "allowed_tools": None,
        "disallowed_tools": DISALLOWED_ALL,
    },
    "hollis": {
        "display": "Holis",
        "token_file": CONFIG_DIR / "telegram-hollis.token",  # absent on purpose — Telegram retired 2026-08-08, HQ-only
        "personality": REPO_DIR / "agents" / "hollis.md",
        "journal": VAULT_HEALTH,
        "journal_title": "Holis + Nat",
        "allowed_tools": HEALTH_ALLOWED,
        "disallowed_tools": DISALLOWED_PA,
        "duo": "health",
    },
    "finn": {
        "display": "Finn",
        "token_file": CONFIG_DIR / "telegram-finn.token",  # absent on purpose
        "personality": REPO_DIR / "agents" / "finn.md",
        "journal": VAULT_FINN,
        "allowed_tools": FINN_ALLOWED,
        "disallowed_tools": DISALLOWED_FINN,
    },
    "drip": {
        "display": "Drip",
        "token_file": CONFIG_DIR / "telegram-drip.token",  # absent on purpose — HQ-only
        "personality": REPO_DIR / "agents" / "drip.md",
        "journal": VAULT_DRIP / "journal",
        "allowed_tools": DRIP_ALLOWED,
        "disallowed_tools": DISALLOWED_FINN,
    },
    "arc": {
        "display": "ARC",
        "token_file": CONFIG_DIR / "telegram-arc.token",  # absent on purpose — HQ-only
        "personality": REPO_DIR / "agents" / "arc.md",
        # Chat-log transcript only — kept out of idea-archive/ itself so the
        # runner's daily YYYY-MM-DD.md doesn't clutter ARC's curated capture
        # notes (bug caught in scrimmage 2026-08-07).
        "journal": HOME / "Documents" / "MyLifeOS" / "MyKnowledgeOS" / "idea-archive" / "chat-log",
        "allowed_tools": ARC_ALLOWED,
        "disallowed_tools": DISALLOWED_FINN,
    },
    "nat": {
        "display": "Nat",
        "token_file": CONFIG_DIR / "telegram-nat.token",  # absent on purpose — Telegram retired 2026-08-08, HQ-only
        "personality": REPO_DIR / "agents" / "nat.md",
