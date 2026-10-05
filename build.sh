#!/bin/bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "=========================================="
echo " Building exFAT Image Builder for macOS   "
echo "=========================================="

if [ -n "$PYTHON" ]; then
    PYTHON_BIN="$PYTHON"
elif [ -f "/opt/homebrew/bin/python3.11" ]; then
    PYTHON_BIN="/opt/homebrew/bin/python3.11"
else
    PYTHON_BIN="$(which python3)"
fi

echo "Using Python: $PYTHON_BIN"
$PYTHON_BIN --version

echo ""
echo "Installing / Verifying dependencies..."
$PYTHON_BIN -m pip install --upgrade \
    pyinstaller \
    pillow \
    tkinterdnd2 \
    psutil \
    "mkpfs==0.0.8" \
    cryptography \
    lz4 \
    toml \
    tkmacosx

ICON_ARG=""
if [ -f "assets/AppIcon.icns" ]; then
    ICON_ARG="--icon=assets/AppIcon.icns"
elif [ -f "controller.ico" ]; then
    ICON_ARG="--icon=controller.ico"
fi

echo ""
echo "Running PyInstaller to produce macOS .app bundle..."
$PYTHON_BIN -m PyInstaller \
    --noconfirm \
    --clean \
    --windowed \
    --name "exFAT Image Builder" \
    $ICON_ARG \
    --osx-bundle-identifier "com.ps5.exfatbuilder" \
    --add-data "assets:assets" \
    --add-data "make_image_mac.py:." \
    --hidden-import PIL._tkinter_finder \
    --hidden-import psutil \
    --hidden-import mkpfs \
    --hidden-import mkpfs.__main__ \
    --hidden-import mkpfs.cli \
    --hidden-import mkpfs.pfs \
    --hidden-import mkpfs.utils \
    --hidden-import mkpfs.consts \
    --hidden-import mkpfs.logging \
    --hidden-import mkpfs.pbar \
    --hidden-import cryptography \
    --hidden-import cryptography.hazmat.primitives.ciphers \
    --hidden-import cryptography.hazmat.primitives.ciphers.algorithms \
    --hidden-import cryptography.hazmat.primitives.ciphers.modes \
    --hidden-import cryptography.hazmat.backends \
    --hidden-import cryptography.hazmat.backends.openssl \
    --hidden-import cryptography.hazmat.backends.openssl.backend \
    --hidden-import tkmacosx \
    --collect-all tkmacosx \
    --collect-all mkpfs \
    --collect-all cryptography \
    --collect-all tkinterdnd2 \
    --collect-all lz4 \
    --collect-all toml \
    --collect-submodules ui \
    --collect-submodules ui.shared \
    --paths . \
    exfat_builder.py

APP_PATH="dist/exFAT Image Builder.app"
if [ -d "$APP_PATH" ]; then
    echo ""
    echo "=========================================="
    echo " Build SUCCESSFUL!"
    echo " Application bundle created at:"
    echo " $APP_PATH"
    echo "=========================================="

    # Ensure executable permissions inside the app bundle
    chmod +x "$APP_PATH/Contents/MacOS/exFAT Image Builder"

    # Create a distributable ZIP archive
    echo "Creating distributable ZIP archive..."
    (cd dist && zip -r -q "exFAT-Image-Builder-macOS.zip" "exFAT Image Builder.app")
    echo "Created: dist/exFAT-Image-Builder-macOS.zip"
else
    echo "ERROR: Application bundle was not created."
    exit 1
fi
