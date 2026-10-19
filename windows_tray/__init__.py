"""Windows system-tray UI for Helm.

Mirrors the macOS menu-bar app's chat flow (Sources/MacMenuAssistant/ChatView.swift):
spawns assistant.py as a subprocess per chat turn and speaks the same
line-prefixed stdout protocol (CHAT_REPLY_JSON:, APPROVAL_REQUIRED:, TOOL:,
CURSOR:). See the project README for what's ported in this first pass versus
deferred (voice input, the 3-agent pipeline view, Logs tab, bridge settings).
"""
