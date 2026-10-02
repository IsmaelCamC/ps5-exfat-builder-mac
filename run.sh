#!/usr/bin/env bash
# run.sh — Script para ejecutar exFAT Image Builder en macOS
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# 1. Buscar intérprete de Python con soporte para Tkinter
CANDIDATES=(
    "/opt/homebrew/bin/python3.11"
    "/opt/homebrew/bin/python3"
    "/usr/local/bin/python3.11"
    "/usr/local/bin/python3"
    "$(which python3 2>/dev/null || true)"
)

PYTHON_BIN=""
for py in "${CANDIDATES[@]}"; do
    if [ -n "$py" ] && [ -x "$py" ]; then
        if "$py" -c "import tkinter" 2>/dev/null; then
            PYTHON_BIN="$py"
            break
        fi
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo "❌ Error: No se encontró una versión de Python 3 con Tkinter instalado."
    echo "💡 Puedes instalarlo con Homebrew ejecutando:"
    echo "    brew install python@3.11 python-tk@3.11"
    exit 1
fi

echo "🚀 Iniciando exFAT Image Builder..."
echo "🐍 Python: $PYTHON_BIN ($($PYTHON_BIN --version))"

# 2. Configurar entorno y ejecutar
export PYTHONPATH="$DIR:${PYTHONPATH}"
exec "$PYTHON_BIN" "$DIR/exfat_builder.py" "$@"
