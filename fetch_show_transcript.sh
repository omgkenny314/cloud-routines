#!/bin/bash
# Fetch the newest episode transcript for one of Kenny's stock shows and
# push it to the cloud-routines repo, where the cloud recap routines read it.
# Usage: fetch-show-transcript.sh market-mondays|trapping-tuesdays
#
# Lives in ~/.local/bin and works in ~/.local/state/cloud-routines-relay —
# both OUTSIDE ~/Documents, because macOS privacy (TCC) blocks launchd jobs
# from reading Documents ("Operation not permitted", the bug that killed the
# original relay from Aug 2 to Aug 20, 2026). The Documents checkout of
# cloud-routines is pull-only (Finn's Island ingest); this clone is the
# push side.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

REPO_DIR="$HOME/.local/state/cloud-routines-relay"
LOG_DIR="$HOME/.local/state/cloud-routines-relay-logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/fetch.log"
SHOW="${1:?usage: fetch-show-transcript.sh market-mondays|trapping-tuesdays}"

# Deployed copy — launchd python cannot read the Documents original (TCC).
# Re-copy from double-o-agents/push_notify.py if that file ever changes.
PUSH_NOTIFY="$HOME/.local/lib/double-o/push_notify.py"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [$SHOW] $*" >> "$LOG"; }

# Silence from a watchdog is a finding: any failure alerts Kenny via HQ push
# (python3 reading Documents from launchd is proven working by the healthcheck).
fail() {
  log "ERROR: $1"
  /usr/bin/python3 "$PUSH_NOTIFY" send "Transcript relay error" \
    "$SHOW relay failed: $1" >> "$LOG" 2>&1
  exit 1
}

case "$SHOW" in
  market-mondays)
    SOURCE_URL="https://www.youtube.com/playlist?list=PLXa8HXFcKT961IieWfhylPvBNeH2cO8dY"
    ;;
  trapping-tuesdays)
    SOURCE_URL="https://www.youtube.com/@wallstreetlookslikeusnow/streams"
    ;;
  *) echo "unknown show: $SHOW" >&2; exit 1 ;;
esac

log "run started"

[ -d "$REPO_DIR/.git" ] || git clone --quiet https://github.com/omgkenny314/cloud-routines.git "$REPO_DIR" \
  || fail "could not clone cloud-routines repo"

OUT_DIR="$REPO_DIR/transcripts/$SHOW"
mkdir -p "$OUT_DIR"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# The newest episode is the first item of the source.
FOUND="$(yt-dlp --flat-playlist --playlist-items 1 --print "%(id)s|%(title)s" "$SOURCE_URL" 2>>"$LOG" | head -1)"
[ -n "$FOUND" ] || fail "could not list newest item from $SOURCE_URL"

VIDEO_ID="${FOUND%%|*}"
TITLE="${FOUND#*|}"
URL="https://www.youtube.com/watch?v=$VIDEO_ID"
log "newest episode: $TITLE ($VIDEO_ID)"

# Same episode as last run: don't re-download or make a churn commit, but do
# still push anything a previous run committed and failed to deliver.
if grep -q "\"video_id\": \"$VIDEO_ID\"" "$OUT_DIR/meta.json" 2>/dev/null; then
  log "transcript already current for $VIDEO_ID"
  cd "$REPO_DIR" || fail "repo dir missing"
  if [ -n "$(git rev-list @{u}..HEAD 2>/dev/null)" ]; then
    git push --quiet >>"$LOG" 2>&1 || fail "git push failed (is the Mac logged into GitHub? run: gh auth login)"
    log "pushed pending commits"
  fi
  log "run finished OK"
  exit 0
fi

META_JSON="$(yt-dlp --skip-download --no-warnings --print "%(upload_date)s|%(duration)s" "$URL" 2>>"$LOG" | head -1)"
UPLOAD_DATE="${META_JSON%%|*}"
DURATION="${META_JSON#*|}"

yt-dlp --skip-download --write-auto-subs --write-subs --sub-lang "en.*,en" \
  --output "$WORK/ep" "$URL" >>"$LOG" 2>&1 || true

VTT="$(ls "$WORK"/ep*.vtt 2>/dev/null | head -1 || true)"
[ -n "$VTT" ] || fail "no subtitles available for $VIDEO_ID (may still be processing)"

cp "$VTT" "$OUT_DIR/latest.vtt"
cat > "$OUT_DIR/meta.json" <<EOF
{
  "show": "$SHOW",
  "video_id": "$VIDEO_ID",
  "title": $(printf '%s' "$TITLE" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'),
  "url": "$URL",
  "upload_date": "$UPLOAD_DATE",
  "duration_seconds": "$DURATION",
  "fetched_at_utc": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
}
EOF

cd "$REPO_DIR" || fail "repo dir missing"
git pull --rebase --quiet >>"$LOG" 2>&1 || true
git add "transcripts/$SHOW/"
if git diff --cached --quiet; then
  log "no changes to commit (transcript already current)"
else
  git commit --quiet -m "transcript: $SHOW $UPLOAD_DATE ($VIDEO_ID)"
fi
if [ -n "$(git rev-list @{u}..HEAD 2>/dev/null)" ]; then
  git push --quiet >>"$LOG" 2>&1 || fail "git push failed (is the Mac logged into GitHub? run: gh auth login)"
  log "pushed transcript for $VIDEO_ID"
fi
log "run finished OK"
