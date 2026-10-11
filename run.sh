#!/bin/bash
# relay-stats/run.sh — one collection pass: direct LMDB scan -> analyze -> www/index.html
# usage: run.sh <relay-name> <strfry-db-dir> [banlist]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RELAY="$1"; DB="$2"; BAN="${3:-}"
# one run at a time (shared with backfill-run.sh, which takes the lock itself and sets RELAY_STATS_LOCKED)
if [ -z "${RELAY_STATS_LOCKED:-}" ]; then
    if ! mkdir "$HERE/.lock" 2>/dev/null; then echo "$(date '+%F %T') skipped: another run/backfill holds $HERE/.lock" >&2; exit 0; fi
    trap 'rmdir "$HERE/.lock"' EXIT
fi
SINCE=$(( $(date +%s) - 32*86400 ))
TMP="$HERE/scan.tsv"
export DYLD_LIBRARY_PATH="${DYLD_LIBRARY_PATH:-/opt/homebrew/lib}"
TZOFF="$("$HERE/venv/bin/python" "$HERE/tzoffset.py")"
export RELAY_STATS_TZ_OFFSET="$TZOFF"
nice -n 10 "$HERE/${RELAY_STATS_SCANNER:-relay-scan}" "$DB" "$SINCE" > "$TMP.new"
mv "$TMP.new" "$TMP"
# event traffic (ephemeral / non-ephemeral / all) from the strfry-ratelimit audit trail (www/traffic.json); opt-in with AUDIT_DIR in
# relay-stats.conf (or RELAY_STATS_AUDIT_DIR), never fatal
AUDIT="${RELAY_STATS_AUDIT_DIR:-$(sed -n 's/^AUDIT_DIR=//p' "${RELAY_STATS_CONF:-$HERE/relay-stats.conf}" 2>/dev/null | tail -1)}"
AUDIT="${AUDIT/#\~/$HOME}"
if [ -n "$AUDIT" ] && [ -d "$AUDIT" ]; then
    nice -n 10 "$HERE/venv/bin/python" "$HERE/traffic.py" "$AUDIT" "$HERE/www" "$HERE/state/traffic" || echo "$(date '+%F %T') traffic stats failed" >&2
fi
nice -n 10 "$HERE/venv/bin/python" "$HERE/analyze.py" "$RELAY" "$TMP" "$HERE/www" ${BAN:+"$BAN"}
# OG image (www/assets/og.png); failure must not break the stats update
nice -n 10 "$HERE/venv/bin/python" "$HERE/og.py" "$HERE/www" || echo "$(date '+%F %T') og.png render failed" >&2
echo "$(date '+%F %T') ok $(grep -c '^E' "$TMP") events"
