#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export ASSISTANT_PROJECT_DIR="$PROJECT_DIR"

DEVELOPER_DIR=$(xcode-select -p)
if [ -d "$DEVELOPER_DIR/SDKs/MacOSX.sdk" ]; then
    SDK_PATH="$DEVELOPER_DIR/SDKs/MacOSX.sdk"
else
    SDK_PATH="$DEVELOPER_DIR/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk"
fi

BUILD_DIR="$PROJECT_DIR/.build"
APP_BUNDLE="$PROJECT_DIR/Helm.app"
CONTENTS="$APP_BUNDLE/Contents"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"
printf 'APPL????' > "$CONTENTS/PkgInfo"
BINARY="$CONTENTS/MacOS/Helm"

# Rebuild only when sources change: macOS ties Screen Recording to the exact
# binary of an ad-hoc signed app, so every needless rebuild revokes it.
if [ ! -x "$BINARY" ] || [ -n "$(find "$PROJECT_DIR/Sources" "$PROJECT_DIR/Resources" -newer "$BINARY" -type f)" ]; then
    cp "$PROJECT_DIR/Resources/Info.plist" "$CONTENTS/Info.plist"
    cp "$PROJECT_DIR/Resources/InfoPlist.strings" "$CONTENTS/Resources/InfoPlist.strings"
    cp "$PROJECT_DIR/Resources/Helm.icns" "$CONTENTS/Resources/Helm.icns"
    swiftc \
        -sdk "$SDK_PATH" \
        -target "$(uname -m)-apple-macosx13.0" \
        -parse-as-library \
        "$PROJECT_DIR/Sources/MacMenuAssistant/main.swift" \
        "$PROJECT_DIR/Sources/MacMenuAssistant/VoiceInput.swift" \
        "$PROJECT_DIR/Sources/MacMenuAssistant/ChatView.swift" \
        -o "$BINARY"

    # A stable certificate keeps permissions across rebuilds; create one named
    # "Helm Dev" in Keychain Access (Certificate Assistant → Code Signing).
    if security find-identity -v -p codesigning | grep -q '"Helm Dev"'; then
        codesign --force --deep --sign "Helm Dev" --identifier local.desktop-assistant "$APP_BUNDLE"
    else
        codesign \
            --force \
            --deep \
            --sign - \
            --identifier local.desktop-assistant \
            --requirements '=designated => identifier "local.desktop-assistant"' \
            "$APP_BUNDLE"
        echo "================================================================"
        echo "AD-HOC SIGNED: every rebuild just invalidated your Screen"
        echo "Recording / Input Monitoring / Accessibility grants for Helm."
        echo "This is a ONE-TIME fix, not something you'll hit again:"
        echo "  1. Open Keychain Access."
        echo "  2. Menu bar: Keychain Access > Certificate Assistant >"
        echo "     Create a Certificate..."
        echo "  3. Name it exactly: Helm Dev"
        echo "  4. Identity Type: Self Signed Root"
        echo "  5. Certificate Type: Code Signing"
        echo "  6. Create, then re-run this script."
        echo "After that, this message won't appear again and today's"
        echo "permission grants will survive every future rebuild."
        echo "================================================================"
    fi
fi

if [ "${1-}" = "--build-only" ]; then
    exit 0
fi

open -a "$APP_BUNDLE"
