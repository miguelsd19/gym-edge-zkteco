#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DIST_DIR="$APP_DIR/dist"
RUNTIME_DIR="$DIST_DIR/runtime"
PLIST_PATH="$HOME/Library/LaunchAgents/com.gymsystem.edge.zkteco.ngrok.plist"
NGROK_PATH="${1:-$(command -v ngrok || true)}"

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$RUNTIME_DIR"

if [[ -z "$NGROK_PATH" || ! -x "$NGROK_PATH" ]]; then
  echo "No se encontro ngrok."
  echo "Pasa la ruta manualmente o instalalo antes de continuar."
  exit 1
fi

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.gymsystem.edge.zkteco.ngrok</string>
  <key>ProgramArguments</key>
  <array>
    <string>$NGROK_PATH</string>
    <string>http</string>
    <string>9877</string>
    <string>--log</string>
    <string>stdout</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$DIST_DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$RUNTIME_DIR/ngrok.stdout.log</string>
  <key>StandardErrorPath</key>
  <string>$RUNTIME_DIR/ngrok.stderr.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl load "$PLIST_PATH"

echo "Servicio instalado en launchd como com.gymsystem.edge.zkteco.ngrok"
echo "plist: $PLIST_PATH"
echo "Revisa la URL publica en $RUNTIME_DIR/ngrok.stdout.log"
