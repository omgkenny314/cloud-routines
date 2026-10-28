#!/bin/bash
# relay-doctor.sh — find out why the show-transcript relay went quiet, fix the
# safe-to-fix parts, and push the findings back to this repo so the cloud
# recap routines can read them.
#
# Run on the Mac, one paste:
#   curl -fsSL https://raw.githubusercontent.com/omgkenny314/cloud-routines/main/relay-doctor.sh | bash
#
# Why this exists: fetch-show-transcript.sh has two paths that fail without
# ever calling fail(), so no HQ push alert is sent and the log still says
# "run finished OK". Those silent paths are the only ones consistent with the
# relay going dark on 2026-08-20 and never alerting. This script looks at
# exactly those paths.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

REPO="$HOME/.local/state/cloud-routines-relay"
LOGS="$HOME/.local/state/cloud-routines-relay-logs"
REPORT="$(mktemp)"
VERDICT=""

say() { echo "$*" | tee -a "$REPORT"; }
note() { VERDICT="$VERDICT
$*"; }

say "relay-doctor $(date '+%Y-%m-%d %H:%M:%S %Z')"
say ""

# --- 1. Does the push-side clone even exist? -------------------------------
if [ ! -d "$REPO/.git" ]; then
  say "FINDING: the relay clone $REPO is MISSING."
  say "Nothing can be pushed. Recreate it:"
  say "  git clone https://github.com/omgkenny314/cloud-routines.git $REPO"
  exit 1
fi
cd "$REPO" || exit 1
say "clone: present at $REPO"

# --- 2. Is the clone wedged mid-rebase? ------------------------------------
# fetch-show-transcript.sh runs `git pull --rebase ... || true`, so a conflict
# leaves the clone detached and the error is swallowed. Every later run then
# fails the same way. This is sticky and silent — it would take out BOTH shows
# at once, which is exactly what happened.
if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then
  say "FINDING: a rebase was left half-finished. This is the silent killer."
  git rebase --abort 2>&1 | tee -a "$REPORT"
  say "repaired: rebase aborted"
  note "- aborted a stuck rebase (this alone would have blocked every run since it started)"
fi
if [ -f .git/MERGE_HEAD ]; then
  say "FINDING: a merge was left half-finished."
  git merge --abort 2>&1 | tee -a "$REPORT"
  note "- aborted a stuck merge"
fi

# --- 3. Detached HEAD? -----------------------------------------------------
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>&1)"
say "branch: $BRANCH"
if [ "$BRANCH" = "HEAD" ]; then
  say "FINDING: HEAD is detached. Commits made here are not on any branch."
  git checkout main 2>&1 | tee -a "$REPORT"
  BRANCH=main
  note "- reattached HEAD to main"
fi

# --- 4. Is the upstream link intact? ---------------------------------------
# `git rev-list @{u}..HEAD 2>/dev/null` in the fetcher prints nothing when
# @{u} is unset, so the push is skipped with no error and no alert.
UPSTREAM="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>&1)"
if echo "$UPSTREAM" | grep -qi 'fatal\|no upstream'; then
  say "FINDING: no upstream set. This is the silent push-skip bug."
  git branch --set-upstream-to=origin/main "$BRANCH" 2>&1 | tee -a "$REPORT"
  note "- restored the missing upstream link (pushes were being skipped silently)"
else
  say "upstream: $UPSTREAM"
fi

# --- 5. Anything stranded locally? -----------------------------------------
git fetch --quiet origin 2>&1 | tee -a "$REPORT"
STRANDED="$(git log --oneline '@{u}..HEAD' 2>/dev/null | wc -l | tr -d ' ')"
say "unpushed commits: $STRANDED"
if [ "${STRANDED:-0}" -gt 0 ]; then
  say "--- stranded commits ---"
  git log --oneline '@{u}..HEAD' 2>&1 | head -30 | tee -a "$REPORT"
  note "- found $STRANDED transcript commit(s) stranded on the Mac; pushing them now"
fi

# --- 6. Are the launchd jobs actually loaded? ------------------------------
# A job that was never bootstrapped is the other silent failure: it cannot
# alert, because it never runs.
say "--- launchd ---"
LOADED="$(launchctl list 2>/dev/null | grep -i 'show-transcript' || true)"
if [ -z "$LOADED" ]; then
  say "FINDING: neither relay job is loaded in launchd. They never fire."
  say "Reload them:"
  say "  launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/com.kennethalford.show-transcript-mm.plist"
  say "  launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/com.kennethalford.show-transcript-tt.plist"
  note "- the launchd jobs are NOT loaded; they have not been running at all"
else
  say "$LOADED"
fi

for S in mm tt; do
  P="$HOME/Library/LaunchAgents/com.kennethalford.show-transcript-$S.plist"
  if [ -f "$P" ]; then
    say "$S schedule: $(/usr/libexec/PlistBuddy -c 'Print :StartCalendarInterval' "$P" 2>/dev/null | tr -d '\n')"
  else
    say "$S plist MISSING at $P"
  fi
done

# --- 7. Tooling and state --------------------------------------------------
say "--- tooling ---"
say "yt-dlp: $(command -v yt-dlp || echo 'NOT FOUND — brew install yt-dlp')"
say "yt-dlp version: $(yt-dlp --version 2>&1 | head -1)"
say "fetcher: $(ls -l "$HOME/.local/bin/fetch-show-transcript.sh" 2>&1 | head -1)"
for S in market-mondays trapping-tuesdays; do
  say "$S meta: $(grep -h 'fetched_at_utc\|upload_date' "transcripts/$S/meta.json" 2>/dev/null | tr -d ' \n' || echo missing)"
done

say "--- last 30 log lines ---"
tail -30 "$LOGS/fetch.log" 2>&1 | tee -a "$REPORT"

# --- 8. Deliver the findings to the cloud ----------------------------------
# If this push lands, the push path is working again and next week's recap
# routine will be able to read everything above.
mkdir -p diagnostics
cp "$REPORT" diagnostics/relay-doctor.txt
git add diagnostics/relay-doctor.txt transcripts/ 2>/dev/null
git commit --quiet -m "relay-doctor: $(date '+%Y-%m-%d')" 2>/dev/null
if git push 2>&1 | tee -a "$REPORT"; then
  say ""
  say "PUSHED. The cloud routine can now read diagnostics/relay-doctor.txt."
else
  say ""
  say "PUSH FAILED — this is the problem. Run: gh auth login"
fi

say ""
say "============ SUMMARY ============"
if [ -z "$VERDICT" ]; then
  say "No broken state found in the clone or in launchd."
  say "That points at the fetch step itself — read the log lines above."
else
  say "Repaired:$VERDICT"
fi
