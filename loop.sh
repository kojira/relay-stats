#!/bin/bash
# relay-stats/loop.sh — run run.sh at a fixed minute of every hour (default :01), so the run right after
# midnight picks up the new day promptly instead of drifting by the run time each hour.
# Start with nohup.
# usage: nohup ~/relay-stats/loop.sh <relay-name> <strfry-db-dir> [banlist] >> ~/relay-stats/loop.log 2>&1 &
# env: RELAY_STATS_INTERVAL (seconds, default 3600), RELAY_STATS_OFFSET (seconds past the interval boundary, default 60)
HERE="$(cd "$(dirname "$0")" && pwd)"
INTERVAL="${RELAY_STATS_INTERVAL:-3600}"
OFFSET="${RELAY_STATS_OFFSET:-60}"
while true; do
    "$HERE/run.sh" "$@" || echo "$(date '+%F %T') run failed"
    now=$(date +%s)
    next=$(( (now - OFFSET) / INTERVAL * INTERVAL + INTERVAL + OFFSET ))
    sleep $(( next - now ))
done
