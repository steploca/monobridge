#!/bin/bash
# Builds Monobridge.app (Apple Silicon) and a zip ready for a GitHub release.
# Usage:  ./build_app.sh        (run from the monobridge folder)
set -e
cd "$(dirname "$0")"

echo "==> Setting up a private Python environment (.venv)"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet pyserial msgpack python-rtmidi pillow rumps pyinstaller

echo "==> Drawing the app icon"
python make_icon.py build_icon.png menubar_on.png menubar_off.png
mkdir -p Monobridge.iconset
for s in 16 32 128 256 512; do
  sips -z $s $s build_icon.png --out Monobridge.iconset/icon_${s}x${s}.png >/dev/null
  sips -z $((s*2)) $((s*2)) build_icon.png --out Monobridge.iconset/icon_${s}x${s}@2x.png >/dev/null
done
iconutil -c icns Monobridge.iconset -o Monobridge.icns
rm -rf Monobridge.iconset build_icon.png

echo "==> Building Monobridge.app"
rm -rf build dist
pyinstaller --noconfirm --windowed --log-level WARN \
  --name Monobridge \
  --icon Monobridge.icns \
  --osx-bundle-identifier org.monobridge.app \
  --hidden-import rtmidi \
  --add-data "menubar_on.png:." \
  --add-data "menubar_off.png:." \
  app.py

PLIST=dist/Monobridge.app/Contents/Info.plist
plutil -replace LSUIElement -bool true "$PLIST"                     # menu-bar only, no Dock icon
plutil -replace CFBundleShortVersionString -string "0.9.1" "$PLIST"
plutil -replace NSHumanReadableCopyright -string "MIT licence. Not affiliated with Monogram." "$PLIST"
codesign --force --deep --sign - dist/Monobridge.app                # ad-hoc signature (required on Apple Silicon)

echo "==> Bringing over existing settings (if any)"
SUP="$HOME/Library/Application Support/Monobridge"
mkdir -p "$SUP"
if [ -f mapping.json ] && [ ! -f "$SUP/mapping.json" ]; then
  cp mapping.json "$SUP/mapping.json"
  echo "    copied mapping.json"
fi

echo "==> Making the release zip"
ditto -c -k --keepParent dist/Monobridge.app dist/Monobridge-mac-AppleSilicon.zip

echo
echo "Done:"
echo "  dist/Monobridge.app                     <- drag to Applications"
echo "  dist/Monobridge-mac-AppleSilicon.zip    <- upload to GitHub Releases"
open dist
