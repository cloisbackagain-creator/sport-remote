#!/usr/bin/env bash
# Launch the Sport Remote desktop bridge and open it in your default browser.
# First run sets up a private Python environment with the two libraries it needs.
set -e
cd "$(dirname "$(readlink -f "$0")")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q bleak aiohttp
fi
exec .venv/bin/python sport_remote.py "$@"
