#!/usr/bin/env bash
# Regenerates screenshots/ from a real run against a fresh server (needs the agent-browser CLI).
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${PORT:-8780}"; DATA="$(mktemp -d)/tasks.json"; S=screenshots; B=(agent-browser --session acs-capture)
python3 -m acs.server --port "$PORT" --data "$DATA" & SERVER=$!
trap 'kill $SERVER 2>/dev/null; "${B[@]}" close >/dev/null 2>&1 || true' EXIT
for _ in $(seq 20); do curl -fs "localhost:$PORT/api/health" >/dev/null && break; sleep 0.3; done
"${B[@]}" open "http://127.0.0.1:$PORT/" >/dev/null; "${B[@]}" set viewport 1360 860 >/dev/null; "${B[@]}" wait 800
"${B[@]}" screenshot $S/01-empty.png >/dev/null
run() { "${B[@]}" fill "#f-title" "$1" >/dev/null; "${B[@]}" select "#f-chaos" "$2" >/dev/null; "${B[@]}" click "button.primary" >/dev/null; }
run "Lumen Coffee" none;  "${B[@]}" wait 1500; "${B[@]}" screenshot $S/02-running.png >/dev/null
"${B[@]}" wait 4500; "${B[@]}" screenshot $S/03-result.png >/dev/null
run "Atlas Bikes" flaky;  "${B[@]}" wait 5500; "${B[@]}" screenshot $S/04-flaky-retries.png >/dev/null
run "Nova Tea" broken;    "${B[@]}" wait 4500; "${B[@]}" screenshot $S/05-failed.png >/dev/null
"${B[@]}" click "#btn-retry" >/dev/null; "${B[@]}" wait 3000; "${B[@]}" screenshot $S/06-recovered.png >/dev/null
run "Orbit Fitness" none; "${B[@]}" wait 1200; "${B[@]}" click "#btn-cancel" >/dev/null; "${B[@]}" wait 800; "${B[@]}" screenshot $S/07-cancelled.png >/dev/null
"${B[@]}" set viewport 390 844 >/dev/null; "${B[@]}" wait 500; "${B[@]}" screenshot --full $S/08-mobile.png >/dev/null
echo "screenshots written to $S/"
