#!/bin/bash
# Builds UsageMeter.app into ~/Applications (no sudo). Needs the Xcode command line tools (swiftc).
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="${1:-$HOME/Applications}/UsageMeter.app"
mkdir -p "$APP/Contents/MacOS"
swiftc -O -o "$APP/Contents/MacOS/UsageMeter" "$HERE/UsageMeter.swift" -framework Cocoa
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>UsageMeter</string>
<key>CFBundleIdentifier</key><string>com.local-first.usage-meter</string>
<key>CFBundleExecutable</key><string>UsageMeter</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSUIElement</key><true/>
</dict></plist>
PLIST
codesign --force --deep -s - "$APP" 2>/dev/null || true
echo "Built: $APP   (open it, or run ./install.sh --meter to start it at login)"
