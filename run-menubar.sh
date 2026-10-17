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
APP_BUNDLE="$BUILD_DIR/LocalDesktopAssistant.app"
CONTENTS="$APP_BUNDLE/Contents"
mkdir -p "$CONTENTS/MacOS"
BINARY="$CONTENTS/MacOS/LocalDesktopAssistant"

# Rebuild only when sources change: macOS ties Screen Recording to the exact
# binary of an ad-hoc signed app, so every needless rebuild revokes it.
if [ ! -x "$BINARY" ] || [ -n "$(find "$PROJECT_DIR/Sources" "$PROJECT_DIR/Resources" -newer "$BINARY" -type f)" ]; then
    cp "$PROJECT_DIR/Resources/Info.plist" "$CONTENTS/Info.plist"
    swiftc \
        -sdk "$SDK_PATH" \
        -target "$(uname -m)-apple-macosx13.0" \
        -parse-as-library \
        "$PROJECT_DIR/Sources/MacMenuAssistant/main.swift" \
        "$PROJECT_DIR/Sources/MacMenuAssistant/VoiceInput.swift" \
        "$PROJECT_DIR/Sources/MacMenuAssistant/ChatView.swift" \
        -o "$BINARY"

    # A stable certificate keeps permissions across rebuilds; create one named
    # "Chatbot Dev" in Keychain Access (Certificate Assistant → Code Signing).
    if security find-identity -v -p codesigning | grep -q '"Chatbot Dev"'; then
        codesign --force --deep --sign "Chatbot Dev" --identifier local.desktop-assistant "$APP_BUNDLE"
    else
        codesign \
            --force \
            --deep \
            --sign - \
            --identifier local.desktop-assistant \
            --requirements '=designated => identifier "local.desktop-assistant"' \
            "$APP_BUNDLE"
        echo "Note: ad-hoc signed. After this rebuild, re-allow Screen Recording for Chatbot."
    fi
fi

if [ "${1-}" = "--build-only" ]; then
    exit 0
fi

open -a "$APP_BUNDLE"
