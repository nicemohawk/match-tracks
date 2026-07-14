#!/usr/bin/env bash
#
# Start the Match Tracks backend for local device/app testing.
#
#   scripts/dev-server.sh
#
# What it does, in order:
#   1. Ensures a Python venv with the locked deps exists (creates it if not,
#      since pipenv isn't installed on this machine).
#   2. Ensures mongod is listening on :27017 (starts it on a fresh 8.0 data dir
#      if nothing is up — avoids the old 6.0 readthis data dir).
#   3. Prints the URL + API key to enter in the app's Settings.
#   4. Runs the Flask dev server in the foreground (ENV=dev → dev auth mode:
#      any APIKey is accepted). Ctrl-C stops the server; mongod keeps running.
#
# Overridable via env: MONGO_DBPATH, MONGO_PORT, FLASK_PORT.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

MONGO_DBPATH="${MONGO_DBPATH:-/opt/homebrew/var/mongodb-8}"
MONGO_PORT="${MONGO_PORT:-27017}"
FLASK_PORT="${FLASK_PORT:-8080}"
VENV_DIR="$REPO_ROOT/.venv"
VENV_PY="$VENV_DIR/bin/python"

log() { printf '\033[1;36m▶ %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

port_listening() { nc -z 127.0.0.1 "$1" >/dev/null 2>&1; }

# --- 1. venv ---------------------------------------------------------------
if [[ ! -x "$VENV_PY" ]]; then
  log "No venv found — creating $VENV_DIR with locked dependencies (one-time)…"
  command -v python3 >/dev/null || die "python3 not found"
  python3 -m venv "$VENV_DIR"
  "$VENV_PY" -m pip install --quiet --upgrade pip
  "$VENV_PY" -m pip install --quiet \
    flask==2.0.1 werkzeug==2.0.1 jinja2==3.0.1 itsdangerous==2.0.1 click==8.0.1 \
    markupsafe==2.0.1 flask-mongoengine==1.0.0 mongoengine==0.23.1 pymongo==3.11.4 \
    marshmallow==3.12.2 flask-httpauth==4.4.0 six==1.16.0 gunicorn==20.1.0 \
    email-validator==1.1.3 flask-wtf==0.15.1 wtforms==2.3.3 \
    "git+https://github.com/benlachman/marshmallow-mongoengine.git@72787fdb5e9d598821df614c676642842ec44b0d"
  log "venv ready."
fi

# --- 2. mongod -------------------------------------------------------------
if port_listening "$MONGO_PORT"; then
  log "mongod already listening on :$MONGO_PORT."
else
  command -v mongod >/dev/null || die "mongod not installed (brew install mongodb-community)"
  log "Starting mongod on :$MONGO_PORT (dbpath: $MONGO_DBPATH)…"
  mkdir -p "$MONGO_DBPATH"
  mongod --dbpath "$MONGO_DBPATH" --port "$MONGO_PORT" \
         --fork --logpath "$MONGO_DBPATH/mongod.log" >/dev/null \
    || die "mongod failed to start — see $MONGO_DBPATH/mongod.log"
  log "mongod started (background; stop with: pkill mongod)."
fi

# --- 3. connection info ----------------------------------------------------
LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo '<your-LAN-IP>')"
cat <<INFO

  In the app's Settings, use:
    Backend URL : http://$LAN_IP:$FLASK_PORT
    API key     : any non-empty string  (dev mode accepts any APIKey)

  Health check : curl http://127.0.0.1:$FLASK_PORT/health

INFO

# --- 4. Flask (foreground) -------------------------------------------------
log "Starting Flask dev server on 0.0.0.0:$FLASK_PORT (Ctrl-C to stop)…"
exec env ENV=dev PYTHONPATH=. PORT="$FLASK_PORT" "$VENV_PY" run.py
