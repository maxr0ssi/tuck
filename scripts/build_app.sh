#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
APP=".build/Tuck.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
xcrun swiftc -swift-version 6 -O macos/DownloadSuggest.swift -o .build/DownloadSuggest-build -framework AppKit -framework UserNotifications -framework PDFKit
mv .build/DownloadSuggest-build "$APP/Contents/MacOS/DownloadSuggest"
cp macos/Info.plist "$APP/Contents/Info.plist"
codesign --force --deep --sign - "$APP" >/dev/null
printf 'Built %s\n' "$APP"
