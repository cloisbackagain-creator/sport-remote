#!/usr/bin/env bash
# Launch Sport Remote: start the Bluetooth bridge and open the app in its own window.
#   ./sport-remote.sh            own window (falls back to the default browser if GTK/WebKit is missing)
#   ./sport-remote.sh --browser  open in the default browser instead
# First run sets up a private Python environment with the libraries the bridge needs.
set -e
cd "$(dirname "$(readlink -f "$0")")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
# bleak: Bluetooth, aiohttp: local web server, segno: QR codes for invite links
.venv/bin/python -c 'import bleak, aiohttp, segno' 2>/dev/null || .venv/bin/pip install -q bleak aiohttp segno

if [ "$1" = "--browser" ]; then
  exec .venv/bin/python sport_remote.py
fi

# start the bridge in the background unless it's already running
if ! (exec 3<>/dev/tcp/127.0.0.1/8766) 2>/dev/null; then
  nohup .venv/bin/python sport_remote.py --no-browser >/tmp/sport-remote-bridge.log 2>&1 &
fi

if /usr/bin/python3 -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("WebKit","6.0")' 2>/dev/null; then
  exec /usr/bin/python3 sport_remote_window.py
else
  sleep 1; xdg-open http://localhost:8766/
fi
