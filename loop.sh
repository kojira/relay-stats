#!/bin/bash
# relay-stats/loop.sh — run run.sh every INTERVAL seconds (default 3600). Start with nohup (launchd cannot read /Volumes due to TCC).
# usage: nohup ~/relay-stats/loop.sh <relay-name> <strfry-db-dir> [banlist] >> ~/relay-stats/loop.log 2>&1 &
HERE="$(cd "$(dirname "$0")" && pwd)"
INTERVAL="${RELAY_STATS_INTERVAL:-3600}"
while true; do
    "$HERE/run.sh" "$@" || echo "$(date '+%F %T') run failed"
    sleep "$INTERVAL"
done
