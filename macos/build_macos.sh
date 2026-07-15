#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="$APP_DIR/.venv/bin/python"
PYINSTALLER_CONFIG_DIR="$APP_DIR/.pyinstaller"

cd "$APP_DIR"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "No se encontro $PYTHON_BIN"
  echo "Crea un entorno virtual en macOS e instala las dependencias primero:"
  echo "  python3 -m venv .venv"
  echo "  .venv/bin/pip install pyzk pyinstaller"
  exit 1
fi

mkdir -p "$PYINSTALLER_CONFIG_DIR"

PYINSTALLER_CONFIG_DIR="$PYINSTALLER_CONFIG_DIR" "$PYTHON_BIN" -m PyInstaller \
  --clean \
  --noconfirm \
  gym-edge-service-zkteco.spec

echo
echo "Build listo en dist/gym-edge-service-zkteco"
echo "Copia tambien tu archivo .env junto al ejecutable."
