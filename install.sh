#!/usr/bin/env bash
# Keephaven Guard Dog installer for Linux and Raspberry Pi OS (64-bit).
# Installs into ~/.local/share/keephaven-guard-dog-app, adds a user service that starts it at boot,
# and opens the dashboard on this machine only (add --network to reach it from your phone or laptop).
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/.local/share/keephaven-guard-dog-app"
PORT="${GD_PORT:-8642}"  # tests only; the dashboard is always on 8642
# ./install.sh                     this machine only
# ./install.sh --network           any device on your network (asks for a password)
# ./install.sh --host ADDR [--allow-host NAME]...   one address only, e.g. your Tailscale IP
HOSTARG="--host 127.0.0.1"
while [ $# -gt 0 ]; do
  case "$1" in
    --network) HOSTARG="--host 0.0.0.0" ;;
    --host) HOSTARG="--host $2"; shift ;;
    --allow-host) HOSTARG="$HOSTARG --allow-host $2"; shift ;;
    *) echo "unknown option $1"; exit 1 ;;
  esac
  shift
done

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || { echo "Needs Python 3.10 or newer."; exit 1; }
python3 -c 'import venv, ensurepip' 2>/dev/null || { echo "Installing python3-venv (asks for your password)"; sudo apt-get install -y python3-venv; }

echo "1/3 Installing Guard Dog and Scrapling"
python3 -m venv "$APP"
"$APP/bin/pip" install -q --upgrade pip
"$APP/bin/pip" install -q "$SRC"

echo "2/3 Installing the hidden browser used by stealth mode (may ask for your password for system libraries)"
"$APP/bin/scrapling" install > "$APP/browser-install.log" 2>&1 || { cat "$APP/browser-install.log"; echo "The browser didn't install (details above)."; exit 1; }

echo "3/3 Starting it at boot"
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/keephaven-guard-dog.service" <<UNIT
[Unit]
Description=Keephaven Guard Dog
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0

[Service]
ExecStart=$APP/bin/keephaven-guard-dog $HOSTARG --port $PORT
Restart=on-failure
RestartSec=15

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable keephaven-guard-dog.service
if systemctl --user is-active --quiet keephaven-guard-dog.service; then
  echo "Restarting Guard Dog (a running scan stops and runs again at the next check)."
fi
STARTED="$(date +%s)"
systemctl --user restart keephaven-guard-dog.service  # a running copy keeps its old settings until it restarts
loginctl enable-linger "$USER" 2>/dev/null || true

# Wait until it's listening (the setup code is written just before), at most GD_WAIT seconds.
LISTEN="$(echo "$HOSTARG" | awk '{print $2}')"; [ "$LISTEN" = 0.0.0.0 ] && LISTEN=127.0.0.1
UP=0
for _ in $(seq 1 "${GD_WAIT:-30}"); do
  if (exec 3<>"/dev/tcp/$LISTEN/$PORT") 2>/dev/null; then UP=1; break; fi
  sleep 1
done
CODE_FILE="${XDG_DATA_HOME:-$HOME/.local/share}/keephaven-guard-dog/setup-code.txt"
if [ "$HOSTARG" != "--host 127.0.0.1" ]; then
  ADDR="$(echo "$HOSTARG" | awk '{print $2}')"; [ "$ADDR" = 0.0.0.0 ] && ADDR="$(hostname -I | awk '{print $1}')"
  if [ "$UP" = 1 ]; then echo "Done. Open http://$ADDR:$PORT"; else echo "Done. Guard Dog is still starting; open http://$ADDR:$PORT in a minute."; fi
  if [ -f "$CODE_FILE" ] && [ "$(stat -c %Y "$CODE_FILE")" -ge "$STARTED" ]; then
    echo "Set a password there. If it asks for a setup code: $(cat "$CODE_FILE")"
  elif [ "$UP" = 0 ]; then
    echo "If it asks for a setup code, it's in $CODE_FILE"
  fi
else
  if [ "$UP" = 1 ]; then echo "Done. Open http://127.0.0.1:$PORT on this machine."; else echo "Done. Guard Dog is still starting; open http://127.0.0.1:$PORT on this machine in a minute."; fi
fi
