# cloud-routines

Support repo for Kenny's Claude cloud routines (scheduled agents at claude.ai/code/routines). Cloud sessions can check out this repo; his private MyLifeOS repo they cannot (403). So anything a routine needs to read lives here.

## Show transcript relay

YouTube blocks transcript downloads from cloud/datacenter IPs, so Kenny's Mac fetches captions at home and this repo couriers them to the cloud.

- `fetch_show_transcript.sh` — pulls the newest episode's subtitles with yt-dlp and pushes them to `transcripts/<show>/latest.vtt` + `meta.json`. Sources: Market Mondays = EYL's official playlist (newest-first); Trappin Tuesdays = the streams tab of youtube.com/@wallstreetlookslikeusnow (the weekly numbered episodes moved there — NOT the main @WallStreetTrapper channel).
- `launchd/` — canonical copies of the Mac launchd jobs that run the fetcher: Market Mondays Tuesday 00:00 Mac-local, Trappin Tuesdays Wednesday 00:00 Mac-local (Mac clock is Pacific; that lands 2:00 AM Central, 30 minutes before the 2:30 AM cloud recap routines). The Mac stays awake via Amphetamine; keep it plugged in.
- The cloud recap routines read `transcripts/<show>/meta.json`, use the transcript if `upload_date` is within 8 days, and fall back to labeled secondhand recaps if the relay missed a night.

Reinstall on a new Mac (or after cleanup), one job at a time:

```
cp launchd/com.kennethalford.show-transcript-mm.plist ~/Library/LaunchAgents/ && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.kennethalford.show-transcript-mm.plist
```

```
cp launchd/com.kennethalford.show-transcript-tt.plist ~/Library/LaunchAgents/ && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.kennethalford.show-transcript-tt.plist
```

Logs: `fetch.log` and `launchd-*.log` in the repo folder (gitignored).

Known clock landmines: the Mac's system clock is Pacific while Kenny lives Central (launchd times compensate), and the cloud routines' cron times are pinned to UTC, so they drift one hour earlier local when daylight saving ends in November. A Todoist reminder exists for the November re-check.

## Relay relocation (2026-08-20)
The transcript relay was dead Aug 2–20: launchd cannot read `~/Documents`
(macOS privacy), so the old plists pointing at a script inside this checkout
failed every week with "Operation not permitted". The relay now lives outside
Documents: script at `~/.local/bin/fetch-show-transcript.sh`, working clone at
`~/.local/state/cloud-routines-relay`, logs at
`~/.local/state/cloud-routines-relay-logs/`, failure alerts via Double-O HQ
push (deployed `~/.local/lib/double-o/push_notify.py`). The copy of the script
in this repo is documentation; the deployed copy is the one that runs. The
Documents checkout of this repo is pull-only (Finn's Island lesson ingest).
The hourly Yahki appointment cloud routine was also retired 2026-08-20,
replaced by a local watcher: `~/.local/bin/yahki-check.sh` (launchd, hourly).
