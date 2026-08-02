#!/bin/bash
# Fetch the newest episode transcript for one of Kenny's stock shows and
# push it to the cloud-routines repo, where the cloud recap routines read it.
# Usage: fetch_show_transcript.sh market-mondays|trapping-tuesdays
# Runs on Kenny's Mac via launchd (home IP can reach YouTube; cloud IPs cannot).

set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

REPO_DIR="/Users/kennethalford/Documents/cloud-routines"
LOG="$REPO_DIR/fetch.log"
SHOW="${1:?usage: fetch_show_transcript.sh market-mondays|trapping-tuesdays}"

# Each show maps to the YouTube source that reliably lists its newest full
# episode first. Market Mondays: EYL's official playlist (newest-first).
# Trapping Tuesdays: the weekly numbered live episodes stream on the
# "Wallstreet Looks Like US Now Network" channel, not @WallStreetTrapper.
case "$SHOW" in
  market-mondays)
    SOURCE_URL="https://www.youtube.com/playlist?list=PLXa8HXFcKT961IieWfhylPvBNeH2cO8dY"
    ;;
  trapping-tuesdays)
    SOURCE_URL="https://www.youtube.com/@wallstreetlookslikeusnow/streams"
    ;;
  *) echo "unknown show: $SHOW" >&2; exit 1 ;;
esac

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [$SHOW] $*" >> "$LOG"; }
log "run started"

OUT_DIR="$REPO_DIR/transcripts/$SHOW"
mkdir -p "$OUT_DIR"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# The newest episode is the first item of the source.
FOUND="$(yt-dlp --flat-playlist --playlist-items 1 --print "%(id)s|%(title)s" "$SOURCE_URL" 2>>"$LOG" | head -1)"

if [ -z "$FOUND" ]; then
  log "ERROR: could not list newest item from $SOURCE_URL"
  exit 1
fi

VIDEO_ID="${FOUND%%|*}"
TITLE="${FOUND#*|}"
URL="https://www.youtube.com/watch?v=$VIDEO_ID"
log "newest episode: $TITLE ($VIDEO_ID)"

# Pull upload date and duration, then just the subtitles (no video download).
META_JSON="$(yt-dlp --skip-download --no-warnings --print "%(upload_date)s|%(duration)s" "$URL" 2>>"$LOG" | head -1)"
UPLOAD_DATE="${META_JSON%%|*}"
DURATION="${META_JSON#*|}"

yt-dlp --skip-download --write-auto-subs --write-subs --sub-lang "en.*,en" \
  --output "$WORK/ep" "$URL" >>"$LOG" 2>&1 || true

VTT="$(ls "$WORK"/ep*.vtt 2>/dev/null | head -1 || true)"
if [ -z "$VTT" ]; then
  log "ERROR: no subtitles available for $VIDEO_ID (may still be processing)"
  exit 1
fi

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

cd "$REPO_DIR"
git pull --rebase --quiet >>"$LOG" 2>&1 || true
git add "transcripts/$SHOW/"
if git diff --cached --quiet; then
  log "no changes to commit (transcript already current)"
else
  git commit --quiet -m "transcript: $SHOW $UPLOAD_DATE ($VIDEO_ID)"
  git push --quiet >>"$LOG" 2>&1
  log "pushed transcript for $VIDEO_ID"
fi
log "run finished OK"
