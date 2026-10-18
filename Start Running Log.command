#!/bin/bash
# Double-click this file to start Running Log on a Mac.
# The first time it sets itself up (a few minutes, needs the internet); after that it just starts.
cd "$(dirname "$0")" || exit 1

say() { printf '\n%s\n' "$1"; }
stop() { say "$1"; echo; read -r -p "Press Return to close this window." _; exit 1; }

# 1. Find Python 3.11 or newer (also in the usual install places, in case this window's PATH misses them).
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3 \
    /Library/Frameworks/Python.framework/Versions/3.1[1-9]/bin/python3 \
    /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(not ((3, 11) <= sys.version_info < (3, 14)))' 2>/dev/null; then
    PYTHON="$candidate"
    break
  fi
done
if [ -z "$PYTHON" ] && [ ! -x .venv/bin/python ]; then
  open "https://www.python.org/downloads/" 2>/dev/null
  stop "Running Log needs Python, which isn't on this Mac yet.
A download page has opened in your browser: install Python from it (version 3.11, 3.12 or 3.13),
then double-click \"Start Running Log\" again."
fi

# 2. First run, or the list of libraries changed since: install what the app needs into its own folder.
WANTED="$(cat requirements.lock requirements.txt 2>/dev/null | shasum | cut -d' ' -f1)"
if [ ! -x .venv/bin/python ] || [ "$(cat .venv/.installed 2>/dev/null)" != "$WANTED" ]; then
  say "Setting up Running Log. This takes a few minutes the first time..."
  if [ ! -x .venv/bin/python ]; then
    "$PYTHON" -m venv .venv || stop "Couldn't prepare the app's folder. Is this folder somewhere you can write, like Documents?"
  fi
  .venv/bin/python -m pip install --quiet --upgrade pip
  # The tested versions first; if one of them isn't available for this Python, whatever is current.
  .venv/bin/python -m pip install --quiet -r requirements.lock ||
    .venv/bin/python -m pip install --quiet -r requirements.txt ||
    stop "Couldn't download what the app needs. Check your internet connection, then double-click \"Start Running Log\" again."
  echo "$WANTED" > .venv/.installed
  say "Set up."
fi

# 3. Start the app and open it in the browser. Closing this window stops it.
say "Starting Running Log..."
PYTHONUNBUFFERED=1 .venv/bin/python -m runapp serve --open || stop "Running Log stopped with an error (shown above)."
