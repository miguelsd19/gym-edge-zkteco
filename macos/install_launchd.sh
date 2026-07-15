#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DIST_DIR="$APP_DIR/dist"
EXE_PATH="$DIST_DIR/gym-edge-service-zkteco"
PLIST_PATH="$HOME/Library/LaunchAgents/com.gymsystem.edge.zkteco.plist"
RUNTIME_DIR="$DIST_DIR/runtime"

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$RUNTIME_DIR"

if [[ ! -x "$EXE_PATH" ]]; then
  echo "No se encontro el ejecutable en $EXE_PATH"
  echo "Primero ejecuta macos/build_macos.sh"
  exit 1
fi

if [[ ! -f "$DIST_DIR/.env" ]]; then
  echo "Aviso: no existe $DIST_DIR/.env"
  echo "Recuerda copiar tu .env junto al ejecutable antes de iniciar el servicio."
fi

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.gymsystem.edge.zkteco</string>
  <key>ProgramArguments</key>
  <array>
    <string>$EXE_PATH</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$DIST_DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$RUNTIME_DIR/launchd.stdout.log</string>
  <key>StandardErrorPath</key>
  <string>$RUNTIME_DIR/launchd.stderr.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl load "$PLIST_PATH"

echo "Servicio instalado en launchd como com.gymsystem.edge.zkteco"
echo "plist: $PLIST_PATH"
