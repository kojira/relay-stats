#!/bin/bash
# keep web/server.mjs running (restart after exit). Environment: see the top of web/server.mjs.
HERE="$(cd "$(dirname "$0")" && pwd)"
while :; do
    /opt/homebrew/bin/node --no-warnings "$HERE/server.mjs" >> "$HERE/web.log" 2>&1
    echo "$(date '+%F %T') server exited ($?), restarting in 5 s" >> "$HERE/web.log"
    sleep 5
done
