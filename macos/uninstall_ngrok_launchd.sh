#!/bin/zsh
set -euo pipefail

PLIST_PATH="$HOME/Library/LaunchAgents/com.gymsystem.edge.zkteco.ngrok.plist"

if [[ -f "$PLIST_PATH" ]]; then
  launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
  rm -f "$PLIST_PATH"
  echo "Servicio removido: com.gymsystem.edge.zkteco.ngrok"
else
  echo "No existe $PLIST_PATH"
fi
