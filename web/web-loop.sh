#!/bin/bash
# keep web/server.mjs running (restart after exit). Started from a shell that can read the external
# disk holding the index; launchd jobs cannot (macOS privacy / TCC) unless node gets Full Disk Access.
# env as in web/launchd/io.kojira.relay-stats-web.plist
HERE="$(cd "$(dirname "$0")" && pwd)"
while :; do
    /opt/homebrew/bin/node --no-warnings "$HERE/server.mjs" >> "$HERE/web.log" 2>&1
    echo "$(date '+%F %T') server exited ($?), restarting in 5 s" >> "$HERE/web.log"
    sleep 5
done
