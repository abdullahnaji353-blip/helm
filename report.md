# Helm Project Audit and Upgrade Report

## Environment and audit

- **Platform:** macOS 26.6, Apple Silicon (arm64).
- **Runtimes:** Swift 6.3.3 and Python 3.9.6.
- **Architecture:** A SwiftUI menu-bar/HUD app launches `assistant.py` as a local
  Python subprocess. The Python backend talks to Ollama by default, optionally
  uses cloud providers, and exposes an allowlisted tool set. Accessibility is
  provided by PyObjC/AXUIElement where possible; screen capture and fallback
  mouse/keyboard actions use PyAutoGUI/Quartz. MCP is a separate stdio JSON-RPC
  connector.
- **Dependencies:** `requirements.txt` has PyAutoGUI and macOS PyObjC bindings,
  plus the Anthropic client dependency. No dependencies were added for this
  upgrade.
- **Report discovery:** The requested root `report.md` did not exist. I read
  `training/REPORT.md` completely; it is the generated report for the last
  model-training sample, not a project architecture report.
- **Existing user change:** `training/curriculum.py` already contained the
  user's private training-data loader. That change was preserved; only the
  Calculator task wording/tool selection was updated.

## Prioritized checklist

| Priority | Status | Work and evidence |
|---|---|---|
| 1. Audit and preserve the current project | **COMPLETE** | Inspected the runtime, dependencies, Swift/Python entry points, existing UI, permissions, tools, tests, README, and training report. Kept the existing model/provider, training code, MCP connector, voice path, and UI architecture. |
| 2. Permission and security system | **IN PROGRESS** | Added a centralized permission/capability inventory to the SwiftUI Permissions screen. It distinguishes macOS TCC grants from browser prompts, limited filesystem access, disabled abilities, and unimplemented admin operations. The backend now rejects tool calls not enabled for the current session. Sensitive screen actions, messages, app installs, and existing app removals require confirmation even in autonomous mode. Common credential/token formats are redacted before history/log persistence; logs are created with user-only permissions. |
| 3. Computer control | **IN PROGRESS** | Existing accessibility-based named controls, screen capture, typing, clicks, scrolling, application launch, and multi-step control remain in place. Added a Calculator action that verifies the app's visible result and tested subtraction/multiplication on the live Mac. Control+Shift+Escape stops the foreground task and all three background agents and interrupts spoken output. Dragging, window management, pause/resume, and durable task recovery are not implemented. The active control path is Python, not the unused Swift device-action helper in `main.swift`. |
| 4. Browser capabilities | **IN PROGRESS** | Existing web search opens Safari/Chrome and reads accessible page results when browser Apple Events allow it. Login and CAPTCHA remain manual. There is no Playwright/WebDriver integration, general tab/window management, or reliable form automation. |
| 5. Planning, memory, and autonomous tasks | **IN PROGRESS** | Existing model tool loops, retry/step limits, chat history, skill memory, activity feed, and per-agent stop controls remain. Added a safety confirmation gate and tool allowlist enforcement. There is no structured resumable task store or task-history browser; the current persisted history is conversation/tool context, not a verified workflow checkpoint. |
| WhatsApp remote access | **IMPLEMENTED; SETUP REQUIRED** | Added an opt-in Meta WhatsApp Business Cloud API bridge, local webhook settings, Keychain secret storage, sender/signature validation, remote task execution, one-time confirmation codes, stop command, and optional local-task notifications. The Mac must remain awake and reachable through a secure HTTPS tunnel. Meta credentials and live webhook delivery have not been configured or tested. |
| 6. UI | **IN PROGRESS** | The existing SwiftUI HUD/chat dashboard, activity stream, agent lanes, voice, provider settings, ability toggles, MCP panel, and permission screen were retained. Added permission capability statuses and an emergency-stop affordance. Dedicated task-history/log browsing and UI automation tests remain. |
| 7. Voice and local AI | **IN PROGRESS** | Ollama remains the default local model; optional cloud providers remain available. Voice recognition requests on-device processing and fails clearly when unavailable. Spoken replies use macOS speech synthesis. Voice selection and true barge-in while speaking are not implemented. |
| 8. Testing and verification | **IN PROGRESS** | Python unit/regression tests cover screen permission failures, tool authorization, sensitive-action denials, provider/MCP behavior, Calculator parsing and visible-result verification, and existing actions. Swift compilation validates the native UI. The live Calculator operation was exercised; general GUI/TCC/browser runtime tests were not performed because they require user-granted permissions, a live desktop session, and/or credentials. |
| 9. Documentation | **COMPLETE** | Added this root audit/status report and updated README setup notes for confirmation, emergency stop, permission categories, and browser limitations. |

## Changes in this upgrade

- `assistant.py`
  - Validates every tool invocation against the actual enabled tool list, so a
    model cannot bypass a disabled ability by returning an unexpected tool
    call.
  - Removes tool argument values from tool-start output to avoid putting
    message text and other arguments in diagnostic logs.
  - Requires an explicit confirmation before sending messages or installing
    apps, including autonomous mode.
  - Requires confirmation for sensitive screen actions such as buying,
    publishing, deleting, or credential/settings changes.
  - Redacts common credential labels and recognizable provider token formats
    from persisted conversation history.
  - Adds a bounded arithmetic tool that validates a single expression, uses
    the actual Calculator UI, and checks the visible result before success.
  - Sends optional task-completion notifications through the configured
    WhatsApp Cloud API; failures are surfaced to the UI.
- `whatsapp_bridge.py`
  - Hosts a loopback-only webhook, verifies Meta HMAC signatures, ignores
    messages from any sender except the configured owner, deduplicates
    webhook IDs, routes remote tasks to a separate chat-history file, and
    relays explicit approval codes and stop requests.
  - Sends text through Meta's Graph API and logs to a user-only rotating log.
- `Sources/MacMenuAssistant/ChatView.swift`
  - Adds a unified permission/capability inventory covering native macOS grants
    and explicitly limited or unsupported capabilities.
  - Groups related desktop/browser/app/system tools under the existing ability
    toggles so a disabled ability removes its tools from the backend session.
  - Adds Control+Shift+Escape to stop all active tasks and speech, and exposes
    the same action in active-task menus. Carbon hotkey handlers now filter by
    registered hotkey ID, and registration failures are surfaced rather than
    silently ignored.
  - Adds an opt-in WhatsApp settings tab; secrets are stored in Keychain and
    the webhook child process starts only when the user explicitly starts it.
  - Redacts common credential/token formats in backend logs, removes raw user
    prompts from the explicit request log entry, and sets log directory/file
    permissions to `0700`/`0600`.
- `tests/test_assistant.py`
  - Adds regressions for disabled-tool enforcement, confirmation during
    autonomous message sending/app installation/sensitive screen actions,
    history redaction, Calculator input validation, and result mismatch.
- `tests/test_whatsapp_bridge.py`
  - Tests Cloud API request construction/failures, webhook signature and sender
    checks, message deduplication, approval codes, and webhook verification.
- `training/curriculum.py`
  - Routes the two-number Calculator exercises through the verified tool while
    preserving the existing private training-data loader.
- `README.md`
  - Documents emergency stop, confirmation behavior, and permission/browser
    limitations, including the Calculator verification boundary.
- `report.md`
  - Records this audit and an honest status checklist.

## Existing permission model and limitations

- Screen capture is governed by macOS Screen Recording TCC consent and normally
  takes effect after restarting Helm.
- Accessibility is a separate macOS grant used for named UI controls, typing,
  keyboard shortcuts, and element discovery. macOS exposes it as one broad
  permission, so Helm cannot split the OS grant into independent keyboard and
  mouse grants.
- The voice shortcut uses Input Monitoring; microphone and Speech Recognition
  permissions are separate.
- Browser automation may trigger macOS Automation consent for the selected
  browser. Reading search results additionally depends on the browser's
  “Allow JavaScript from Apple Events” developer setting.
- General filesystem tools and administrator-level actions are not built in.
  Configured MCP servers are separate processes and may have additional access
  under the user's account; each tool call is confirmed, but users must still
  configure only trusted servers with narrow access.
- API keys configured in the UI are stored in macOS Keychain. The new
  persistence redaction is pattern-based, not a guarantee for arbitrary
  unlabelled secrets. Do not paste passwords or secrets into chat; clear chat
  history and `~/Library/Logs/Helm/backend.log` before sharing diagnostics if
  sensitive content may have appeared.
- The app bundle is ad-hoc signed by the development script. Rebuilds can
  require re-granting macOS Screen Recording/Accessibility consent to the
  current app identity.
- `~/Library/Logs/Helm/backend.log` currently has no size-based rotation or
  retention policy.
- The existing uncommitted `training/curriculum.py` config loader falls back
  on missing/invalid JSON but does not validate the types or required fields of
  valid JSON. It was left unchanged to preserve the user's in-progress change.
- `DeviceActionController` in `Sources/MacMenuAssistant/main.swift` is not
  connected to the active chat/Python tool path; the implemented controller
  remains the Python process.

## Training report findings

`training/REPORT.md` records an earlier sample in which Calculator subtraction
and multiplication failed because the model reported “Done” without the
expected state. The dedicated tool now performs the interaction and result
check itself. I exercised `3 - 9` and `3 × 5` in the real Calculator; both
displayed results matched (`-6` and `15`). The historical training report is
unchanged; the full model-driven training run was not repeated.

## Verification performed

- Python tests: `.venv/bin/python -m unittest discover -s tests -p
  'test_assistant.py' -q` — **69 passed**; WhatsApp bridge tests — **10
  passed**.
- Training curriculum: `PYTHONPATH=.:training .venv/bin/python
  training/curriculum.py` — **1,452 tasks built**, including the revised
  Calculator tasks.
- Swift app build: `sh ./run-menubar.sh --build-only` — **succeeded**. It
  emits existing warnings about a deprecated app-launch API and `AgentRun`
  actor isolation; the app bundle still compiles.
- Whitespace check: `git diff --check` — **passed**.
- Native app permission probe (`--check-permissions`) — **AX=true,
  SCREEN=true, LISTEN=true** for the built `.build` app identity at test time.
- Live screen-capture smoke test (`.venv/bin/python assistant.py
  --test-screen-capture`) — **SCREEN_CAPTURE_OK:3274x2048**; the image was
  immediately discarded.
- Live Calculator smoke test: subtraction returned verified `-6`;
  multiplication returned verified `15`.
- WhatsApp tests exercised a real local HTTP webhook handshake and mocked the
  Graph API. No Meta account, business number, permanent token, public tunnel,
  or outbound delivery credentials were available, so end-to-end WhatsApp
  delivery remains unverified.
- The broader live GUI/browser task flow was not exercised; the permission
  probe only checks macOS preflight status and is not a substitute for all
  action paths.

## Recommended next work

1. Add a safe local test application and verify screen capture, Accessibility,
   mouse/keyboard actions, denial paths, and emergency stop on the actual Mac.
2. Run the full model-driven Calculator curriculum against the new verified
   tool; only the direct live smoke cases have been tested so far.
3. Add independently enforced keyboard/mouse capability gates for both
   accessibility tools and `control_screen` action types.
4. Implement browser automation with accessibility-first selectors, bounded
   retries, and explicit manual-authentication/CAPTCHA handoff.
5. Add cancellable/resumable task records and user controls for saved history.
6. Add automated SwiftUI tests and UI-level tests for permissions, emergency
   stop, and responsive layouts.

## Addendum — this session

Since the audit above, these were added, each tested (Python: unit tests;
Swift: build check) and most verified live against real apps/files:

| Feature | Status |
|---|---|
| Telegram bridge (own file, polling, pairing mode, optional HTTP proxy) | DONE |
| System prompt shrink (long example wall broke tool-calling on gemma4:e2b) | DONE |
| "Continue working, don't just ask" rule for multi-step tasks | DONE |
| `window_control` (move/resize/snap/minimize/close) | DONE, live-verified |
| `file_operations` (list/read/copy/move/rename/delete, home-scoped, Trash) | DONE, live-verified |
| Voice barge-in (talking over Helm stops it) | DONE, build-verified only (no mic in this environment) |
| `drag_in_app` (in-app drag-and-drop) | DONE; Finder file-drag itself has a known macOS synthetic-input limitation — use `file_operations` for moving files |
| Settings -> Logs tab | DONE |
| Pause/resume for interrupted `control_screen` tasks (`resume_task` tool) | DONE, test-verified |
| Playwright replaces Accessibility-based reading for web tasks (`browser_automation.py`) | DONE, live-verified against a real page (navigate + click) |

All 8 items from the plan are now done. Non-web app control (Mail, Finder,
Discord, etc.) is unaffected — Playwright only drives browsers, so
`screen_elements.py`'s Accessibility API is still how Helm sees and acts on
everything that isn't a webpage.

**User-confirmed:** Control+Shift+§ opens the HUD from anywhere, instantly,
with no issues. While confirming this, found and fixed a related gap: if the
OS ever refuses to bind that shortcut or Control+Shift+D (dictation) — e.g.
another app already grabbed it — Helm previously failed silently. It now
shows an error line (as the emergency-stop shortcut already did) naming the
fallback (menu bar / mic button).

**Discord bridge added** (`discord_bridge.py`): polls one configured channel
via Discord's REST API with the same APPROVE/DENY/STOP flow, owner-only
allowlist, and remote-task pattern as the Telegram and WhatsApp bridges.
Bots can't discover new DMs without a persistent Gateway websocket
connection, so this watches a server channel instead of a DM — set up via
the new Discord tab in Settings. Slack bridge is the one remaining item from
the original "fix Telegram, then Discord, Slack, WhatsApp" request.
