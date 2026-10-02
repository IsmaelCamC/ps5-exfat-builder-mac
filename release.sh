#!/usr/bin/env bash
set -e

VERSION="3.6.4"
APP_NAME="exFAT Image Builder"
APP_BUNDLE="dist/${APP_NAME}.app"
RELEASE_DIR="dist/release"
DMG_NAME="exFAT-Image-Builder-v${VERSION}-macOS.dmg"
ZIP_NAME="exFAT-Image-Builder-v${VERSION}-macOS.zip"

echo "=========================================================="
echo " Preparing macOS Release v${VERSION} for ${APP_NAME}"
echo "=========================================================="

if [ ! -d "$APP_BUNDLE" ]; then
    echo "App bundle not found at $APP_BUNDLE. Running build.sh first..."
    ./build.sh
fi

echo ""
echo "[1/5] Updating Info.plist metadata (v${VERSION})..."
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString ${VERSION}" "${APP_BUNDLE}/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :CFBundleVersion string ${VERSION}" "${APP_BUNDLE}/Contents/Info.plist" 2>/dev/null || \
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion ${VERSION}" "${APP_BUNDLE}/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :NSHumanReadableCopyright string 'PS5 exFAT Image Builder for macOS'" "${APP_BUNDLE}/Contents/Info.plist" 2>/dev/null || true

echo "[2/5] Setting binary permissions and ad-hoc code signature..."
chmod +x "${APP_BUNDLE}/Contents/MacOS/${APP_NAME}"
xattr -cr "${APP_BUNDLE}"
codesign --force --deep --sign - "${APP_BUNDLE}"

echo "[3/5] Cleaning and creating release directory..."
rm -rf "$RELEASE_DIR"
mkdir -p "$RELEASE_DIR"

echo "[4/5] Generating DMG installer..."
DMG_STAGING="dist/dmg_staging"
rm -rf "$DMG_STAGING"
mkdir -p "$DMG_STAGING"
cp -R "${APP_BUNDLE}" "$DMG_STAGING/"
ln -s /Applications "$DMG_STAGING/Applications"

hdiutil create \
    -volname "${APP_NAME}" \
    -srcfolder "$DMG_STAGING" \
    -ov \
    -format UDZO \
    "${RELEASE_DIR}/${DMG_NAME}"

rm -rf "$DMG_STAGING"

echo "[5/5] Generating ZIP distribution archive..."
(cd dist && zip -r -q "release/${ZIP_NAME}" "${APP_NAME}.app")

echo ""
echo "Calculating SHA-256 checksums..."
(cd "$RELEASE_DIR" && shasum -a 256 "$DMG_NAME" "$ZIP_NAME" > SHA256SUMS.txt)

echo ""
echo "=========================================================="
echo " Release v${VERSION} successfully generated in ${RELEASE_DIR}!"
echo "=========================================================="
ls -lh "$RELEASE_DIR"
cat "${RELEASE_DIR}/SHA256SUMS.txt"
