#!/bin/bash
# keep web/server.mjs running (restart after exit). Environment: see the top of web/server.mjs;
# WEB_NODE = node binary (default: node on PATH).
HERE="$(cd "$(dirname "$0")" && pwd)"
NODE="${WEB_NODE:-$(command -v node)}"
[ -x "$NODE" ] || { echo "web-loop.sh: node not found (set WEB_NODE)" >&2; exit 1; }
while :; do
    "$NODE" --no-warnings "$HERE/server.mjs" >> "$HERE/web.log" 2>&1
    echo "$(date '+%F %T') server exited ($?), restarting in 5 s" >> "$HERE/web.log"
    sleep 5
done
