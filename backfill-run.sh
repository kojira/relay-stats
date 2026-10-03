#!/bin/bash
# full-history recompute: backfill-run.sh <relay> <db> [banlist]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export DYLD_LIBRARY_PATH="${DYLD_LIBRARY_PATH:-/opt/homebrew/lib}"
export RELAY_STATS_TZ_OFFSET="$("$HERE/venv/bin/python" "$HERE/tzoffset.py")"
nice -n 10 "$HERE/relay-scan" "$2" 1669852800 "$(date +%s)" full > "$HERE/full.tsv"
rm -f "$HERE/www/history/"*.json
"$HERE/venv/bin/python" "$HERE/backfill.py" "$1" "$HERE/full.tsv" "$HERE/www" ${3:-}
"$HERE/run.sh" "$@"
rm -f "$HERE/full.tsv"
echo "$(date '+%F %T') backfill done"
