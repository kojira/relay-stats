#!/bin/bash
# full-history recompute: backfill-run.sh <relay> <db> [banlist]
# History is rebuilt in www/history.new and swapped in only after backfill succeeds.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# wait for a running loop pass to finish, then hold the lock for the whole recompute
until mkdir "$HERE/.lock" 2>/dev/null; do sleep 10; done
trap 'rmdir "$HERE/.lock"' EXIT
export RELAY_STATS_LOCKED=1
export DYLD_LIBRARY_PATH="${DYLD_LIBRARY_PATH:-/opt/homebrew/lib}"
TZOFF="$("$HERE/venv/bin/python" "$HERE/tzoffset.py")"
export RELAY_STATS_TZ_OFFSET="$TZOFF"
nice -n 10 "$HERE/relay-scan" "$2" 1669852800 "$(date +%s)" full > "$HERE/full.tsv"
# backfill.py writes <outdir>/history and <outdir>/lang-cache.json; build in a staging dir
STAGE="$HERE/www.backfill"
rm -rf "$STAGE"; mkdir -p "$STAGE"
"$HERE/venv/bin/python" "$HERE/backfill.py" "$1" "$HERE/full.tsv" "$STAGE" ${3:+"$3"}
rm -rf "$HERE/www/history.old"
[ -d "$HERE/www/history" ] && mv "$HERE/www/history" "$HERE/www/history.old"
mv "$STAGE/history" "$HERE/www/history"
mv "$STAGE/lang-cache.json" "$HERE/www/lang-cache.json"
rm -rf "$STAGE" "$HERE/www/history.old"
"$HERE/run.sh" "$@"
rm -f "$HERE/full.tsv"
echo "$(date '+%F %T') backfill done"
