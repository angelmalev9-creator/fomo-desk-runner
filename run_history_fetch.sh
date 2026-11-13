#!/usr/bin/env bash
# Supervisor: restart the fetcher until it reports done (exit 0). Safe to run twice? No: use the pidfile.
cd /workspace/desk
echo $$ > logs/history_fetch.pid
while true; do
  .venv/bin/python history_fetch.py --gap "${GAP:-1.2}"
  rc=$?
  [ "$rc" = "0" ] && { echo "$(date '+%F %T') fetch done" >> logs/history_fetch.log; break; }
  echo "$(date '+%F %T') fetcher exited rc=$rc, restarting in 30s" >> logs/history_fetch.log
  sleep 30
done
rm -f logs/history_fetch.pid
