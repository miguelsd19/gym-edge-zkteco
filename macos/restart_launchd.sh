#!/bin/zsh
set -euo pipefail

EDGE_PLIST="$HOME/Library/LaunchAgents/com.gymsystem.edge.zkteco.plist"
NGROK_PLIST="$HOME/Library/LaunchAgents/com.gymsystem.edge.zkteco.ngrok.plist"

if [[ -f "$NGROK_PLIST" ]]; then
  echo "Reiniciando com.gymsystem.edge.zkteco.ngrok"
  launchctl unload "$NGROK_PLIST" >/dev/null 2>&1 || true
  launchctl load "$NGROK_PLIST"
else
  echo "No existe $NGROK_PLIST. Se omite ngrok."
fi

if [[ -f "$EDGE_PLIST" ]]; then
  echo "Reiniciando com.gymsystem.edge.zkteco"
  launchctl unload "$EDGE_PLIST" >/dev/null 2>&1 || true
  launchctl load "$EDGE_PLIST"
else
  echo "No existe $EDGE_PLIST. Se omite edge."
fi

echo "Reinicio solicitado."
