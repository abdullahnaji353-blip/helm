# Local Desktop Assistant

A local-first desktop assistant prototype (macOS and Windows). It uses Ollama
for local language and vision models and offers a Terminal interface plus a
native menu-bar/tray interface for task-scoped desktop control.

## Requirements

### macOS
- macOS
- Python 3.9 or newer
- Xcode Command Line Tools for the menu-bar interface
- [Ollama](https://ollama.com/) with a model downloaded locally
- PyAutoGUI for optional desktop control

### Windows
- Windows 10 or newer
- Python 3.9 or newer
- [Ollama](https://ollama.com/) with a model downloaded locally
- PyAutoGUI plus `pywinauto`/`pywin32` for screen control (installed
  automatically from `requirements.txt`)

The Windows tray UI is a first pass: chat, task execution, the approval flow,
and screen control/app discovery are ported. Voice input, the 3-background-
agent pipeline view, the Logs tab, bridge (WhatsApp/Telegram/Discord/Slack)
settings, and global hotkeys are not yet — see
`platform_compat.py`/`screen_elements_win.py`/`app_discovery_win.py` for what
each covers.

## Get started

1. Install Ollama and start it.
2. In Terminal, download the default model:

   ```sh
   ollama pull llama3.2
   ```

3. From this folder, run:

   ```sh
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install -r requirements.txt
   python assistant.py
   ```

To launch the native menu-bar interface instead:

```sh
bash run-menubar.sh
```

You can also double-click `Open Helm.command` in Finder to launch it.

### Windows

**Pre-built executable (no Python install needed):** push to `main` triggers
`.github/workflows/build-windows.yml`, which builds `Helm.exe` and
`assistant.exe` on a real Windows GitHub Actions runner (PyInstaller cannot
cross-compile, so this has to run on actual Windows). Download the
`helm-windows` artifact from the workflow's latest run under this repo's
**Actions** tab, unzip it, and double-click `Helm.exe`.

**From source:**

1. Install [Ollama](https://ollama.com/) and start it, then pull a model:

   ```powershell
   ollama pull llama3.2
   ```

2. From this folder, in PowerShell:

   ```powershell
   powershell -ExecutionPolicy Bypass -File run-menubar.ps1
   ```

   This creates `.venv`, installs `requirements.txt` (which pulls in the
   Windows-only `pywinauto`/`pywin32`/`psutil`/`pyttsx3`/`pystray` packages),
   and starts the tray icon. Right-click or left-click the tray icon and
   choose **Open Chat**.

   To run the terminal chat instead of the tray UI:

   ```powershell
   .venv\Scripts\python.exe assistant.py
   ```

The launcher compiles the small Swift app with the selected Xcode Command Line
Tools and starts it. The first launch may take a little while to compile.

It appears as a sparkle icon in the menu bar. Enter a task, choose **Ask for
approval** or **Act autonomously**, then press **Start task**. Approval mode
pauses before each proposed mouse or keyboard action. Autonomous mode performs
those actions without per-action prompts for this task only. The setting resets
to approval mode when the task finishes. Use **Stop** to terminate the current
task, or press **Control+Shift+Escape** to stop the chat task and all three
background agents and interrupt spoken output. Sending messages, installing
apps, uninstalling apps, and screen actions that appear to buy, delete, publish,
or change credentials/settings still require confirmation, even in autonomous
mode. Simple requests such as “Open Safari” use macOS's direct app launcher
instead of screen-click automation.

Press **Control+Option+Space** to open or bring Helm forward. Helm must already
be running in the menu bar; this shortcut works without Input Monitoring.
Modifier-only **Control+Option** is an optional gesture: tap it to open Helm or
hold it while speaking, then release to submit the transcription. That gesture
requires Input Monitoring. In Settings → Abilities, use **Test voice shortcut**
to confirm macOS detects it. Speech recognition is configured to run on-device;
if macOS does not support on-device recognition for the selected language, the
app reports that and does not send microphone audio elsewhere. The first voice
use asks for Microphone and Speech Recognition permissions. Accessibility is
needed separately for operating other apps, not for opening Helm. Grant these
permissions to Helm in System Settings → Privacy & Security.
In **Abilities**, **Applications** enables viewing every running app (including
apps without visible windows), opening and quitting apps, and managing installs.
It can also list installed apps and `.app` bundles in Downloads with their paths
and running status, and open any uniquely named bundle. A downloaded app is not
reported as installed until it is in an Applications folder. macOS may still
show its own first-open security prompt. Helm always asks before installing or
removing an app. **Screen control** uses
Accessibility to inspect controls, drag items, and move, resize, or close
windows; Screen Recording lets it see the display. Click coordinates are derived
from the current screen and are not saved, since window positions and displays
can change.
The menu-bar microphone button is also available for starting and stopping
voice capture manually.
The microphone beside the chat keyboard field dictates into the draft, so you
can review and edit the transcript before sending. **Control+Shift+D** starts
the same dictation mode.

### Background agents

The three background lanes have different specialties: **Researcher** finds
evidence and cites sources, **Operator** performs the assigned task using the
existing approval checks, and **Verifier** checks results and reports gaps.
Researcher and Verifier have read-oriented tool allowlists; only Operator gets
the full task-action toolset, with the same sensitive-action confirmations.
Each task shows a short generated plan and live tool/checkpoint activity. Each
agent keeps its own conversation history between tasks; start a new chat to
clear the main conversation, or remove the per-agent history files from
`~/Library/Application Support/Local Desktop Assistant/` to reset agent memory.
Agent planning uses the selected model provider, so cloud-provider tasks send
their task text there; local Ollama tasks stay on the Mac.
If a workflow is interrupted, resuming instructs the agent to inspect its saved
conversation and current state before continuing, and to ask you rather than
repeat a side effect it cannot verify. Screen and browser tasks also save each
successful action and persist the action about to run; if Helm restarts during
an action, it asks the agent to inspect the current state before retrying.
When browser automation sees multiple controls with the same accessible name,
it stops instead of automatically choosing the first one.

Select **Search the web for examples** to opt in to a browser search (Chrome or
Safari) before a typed or spoken task. The task text is searched on Google and the results are
shown in Activity; only titles, links, and snippets are supplied to the local
vision model. Web results are untrusted reference material, not instructions.
Leave the option off to keep the task query local.

For web tasks in Helm's Playwright browser, Helm also reads up to 4,000
characters of visible page text and labeled controls to help it research and
fill forms. Page text is untrusted and is sent to the selected model provider;
Helm still asks before submitting, posting, or sending. Completed browser tasks
include a short list of pages visited; query strings and URL credentials are
excluded from those links.

Basic two-number arithmetic can use the dedicated Calculator tool. It clears
the real Calculator app, enters the expression, and checks the visible result
before reporting success. This does not provide general browser automation:
web search opens search pages, while login, CAPTCHA, and other restricted
interactions remain manual.

In approval mode, **Allow All** approves the current proposed action and
continues the current task without further per-action prompts. It resets when
the task ends. This is not unrestricted computer control: the prototype still
only performs visible clicks, typing, key presses, and scrolling.

### MCP servers

Open **MCP Tools** in the menu-bar app, enter a trusted server configuration
using the example below, and press **Save & Connect**. The config is stored
in `~/Library/Application Support/Local Desktop Assistant/mcp_servers.json`,
not in the project repository. If no configuration exists yet, the panel
prefills a local `ping` test server. Press **Save & Connect**, select `ping`,
leave arguments as `{}`, then run it and confirm; the result should be `pong`.
To add the optional filesystem server, add a separate entry such as:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": [
        "-y",
        "@modelcontextprotocol/server-filesystem@1.0.0",
        "/Users/your-name/Documents"
      ]
    }
  }
}
```

Replace `/Users/your-name/Documents` with a real directory you intend to
expose. The server needs Node.js/npx installed and will have access to that
directory.

Connected tools are listed in the panel. Select a tool, enter its arguments as
a JSON object, and choose **Review & Run Tool**; the app presents a confirmation
before each tool call. MCP servers are separate programs and run with your
account's permissions, so only configure servers you trust and grant narrow
directory/service access. The connector currently supports local stdio
servers; remote HTTP MCP transports are not implemented.
The example pins the filesystem package to version `1.0.0` so a later package
release is not silently selected. `npx -y` still downloads and runs that
package from npm the first time you connect; review the pinned package and its
permissions before using it, and expose only a directory you are comfortable
granting it access to.

The connector uses MCP's JSON-RPC stdio protocol directly to remain compatible
with the existing Python 3.9 environment. The official
[Model Context Protocol Python SDK](https://github.com/modelcontextprotocol/python-sdk)
is a good upstream reference for clients, but its current main branch requires
Python 3.11 or newer.

MCP diagnostics are written by Python to
`~/Library/Logs/Local Desktop Assistant/mcp.log`; the menu-bar app also writes
launch and result summaries to Unified Logging. To inspect the Swift logs:

```sh
log show --info --last 30m --style compact --predicate 'subsystem == "local.desktop-assistant" AND category == "mcp"'
```

The Python log records process starts, protocol request/response stages, tool
names, and errors, but omits tool argument values and result contents.

The PyAutoGUI dependency is used only for desktop control. To reopen the
assistant in a new Terminal window later, activate the environment first:

```sh
cd /Users/a.naji/new
source .venv/bin/activate
python assistant.py
```

To use another model that is already installed in Ollama:

```sh
OLLAMA_MODEL=your-model-name python3 assistant.py
```

Chat prompts and desktop screenshots go only to Ollama on this Mac
(`localhost`). No API key is required. Desktop-control Python packages are
installed into the project virtual environment.

## AI providers and API keys

Helm runs on local Ollama by default. To use a cloud model instead, open
**AI model & API keys…** in the settings menu (or the first-launch setup) and
pick Claude, ChatGPT, Gemini or OpenRouter, then paste your key. Keys are
stored in the macOS Keychain (service "Chatbot API Keys"), never in this
folder, and are handed to the Python backend only through the
`HELM_API_KEY` environment variable of each request.
Conversation history and the backend activity log remain local. Common
credential labels and recognizable token formats are redacted before saving,
but this is not a general-purpose secret detector; do not paste passwords or
tokens into chat. The activity log is stored at
`~/Library/Logs/Helm/backend.log` with user-only permissions.

From Terminal you can use the usual variables instead:

```sh
ANTHROPIC_API_KEY=... python assistant.py --chat "hello" --provider anthropic
OPENAI_API_KEY=... python assistant.py --chat "hello" --provider openai --model gpt-5-mini
```

`--check-key` verifies a key without chatting. `--cloud-vision` sends screen-control screenshots to the chosen cloud model
(much more accurate than the small local model). `--disable-tools web_search` turns off the
web search tool, which opens a Google, YouTube or TikTok search in Chrome or
Safari (`--browser chrome|safari`) and reads the result links when the browser
allows JavaScript from Apple Events.

## WhatsApp remote access

To jump to a connection page, ask Helm to open that service's settings, for
example, “Open Telegram settings.” Helm opens the matching page and shows
whether the bridge is running. Finish account setup with the service itself,
then enter tokens only in Helm's secure settings fields—not in chat. macOS
privacy permissions must be granted separately by the user in System Settings.
For a simple launch request such as “Open Safari” or “Open Discord,” Telegram
uses Helm's app launcher directly and reports the launcher's actual result.

The menu-bar app includes an opt-in **WhatsApp** settings tab for Meta's
WhatsApp Business Cloud API. This is not a direct link to a personal WhatsApp
session: you need a Meta developer app with WhatsApp Cloud API, a phone number
ID, a permanent access token, the app secret, and a webhook verification
token. Your personal WhatsApp number must be added as an allowed recipient in
Meta's setup and entered in E.164 digits without `+`.

1. Enter the Phone Number ID and your recipient number in Settings → WhatsApp.
2. Save the access token, Meta app secret, and a unique verify token of at
   least 24 letters, digits, `_` or `-`. Secrets are stored in Keychain.
3. Start the local webhook. It binds only to `127.0.0.1:8765`.
4. Install and run Cloudflare Tunnel, for example
   `cloudflared tunnel --url http://127.0.0.1:8765`.
5. In Meta Webhooks, use `https://YOUR-TUNNEL-HOST/webhook`, enter the same
   verify token, and subscribe to the WhatsApp `messages` field. The quick
   tunnel hostname may change when restarted.
6. Send a text from your configured number. Helm processes it as a remote task
   using the selected model and enabled abilities. Send `STOP` to cancel an
   active task. When an action needs approval, reply `APPROVE <code>` or
   `DENY <code>` within 90 seconds.

The Mac, Helm and tunnel must remain awake and connected. The webhook verifies
Meta's HMAC signature, accepts commands only from the configured sender, and
keeps remote chat history separate from local chat history. Remote messages
are processed by Meta and the tunnel provider; model requests follow the
selected provider's local/cloud behavior. Destructive or sensitive actions are
not performed without a one-time WhatsApp approval code. Proactive messages
from local tasks may require an approved WhatsApp message template outside the
24-hour customer-service window. Setup requires a Meta account and was not
live-tested without credentials.

## Commands

- Type a message to chat with the local model. Replies are spoken by default.
- `/speak off` disables spoken replies; `/speak on` enables them again.
- Copy text, then use `/read` to speak the clipboard contents. The clipboard is
  only read after you explicitly run this command.
- `/open Safari`, `/open Notes`, `/open Calendar`, `/open Calculator`, or
  `/open Finder` opens an installed app or a downloaded `.app` bundle by name.
- `/open-url https://example.com` opens an HTTP or HTTPS URL in the browser.
- `/do <task>` asks a local vision model to perform a task using the screen.
  It proposes one click, keystroke, typing, or scrolling action at a time and
  asks you to approve each action. Type `y` or `yes` to allow it; any other
  response stops the task. It stops automatically after 20 steps.
- `/help` lists commands; `/quit` exits.

The model cannot run shell commands. Autonomous mode performs proposed desktop
actions without asking, so use it only when you are comfortable with the task's
effects. Screenshots are sent only to Ollama on this Mac, but can contain
sensitive information; close private content before starting a desktop task.
The assistant does not store screenshots itself. You can stop input immediately
by moving the pointer to a screen corner or using the menu's **Stop** button.

### Enable desktop mode

Desktop mode needs a vision-capable local model. The default is
`gemma4:e2b`. If it is not installed, download it with:

```sh
ollama pull gemma4:e2b
```

In the menu-bar app's **Control** mode, check the Screen Recording status.
Use **Request Access** or **Open System Settings** to enable access under
**System Settings → Privacy & Security → Screen & System Audio Recording**
(named **Screen Recording** on some macOS versions). Enable Local Desktop
Assistant, then refresh the status. If capture fails, choose **Test Capture**:
it exercises the same Python screen-capture path used by Control and reports
success or the actual error in Activity. Quit and reopen the assistant after
changing access. Accessibility access may also be needed for approved mouse
and keyboard actions. These permissions can be revoked from the same settings
page. Start the assistant and try:

```text
/do Open Safari and go to example.com
```

The **Permissions** screen reports native macOS grants separately from
capabilities that are limited or not implemented. Keyboard/mouse interaction
uses Accessibility; screen analysis uses Screen Recording. Browser search uses
the selected browser and may require macOS Automation consent; reading results
also requires enabling “Allow JavaScript from Apple Events” in that browser's
developer settings. Helm does not bypass logins, CAPTCHA, or access controls.
General filesystem operations and administrator-level operations are not
implemented.

### Accessibility diagnostics

At startup the menu-bar app logs its own `AXIsProcessTrusted` result, bundle
identifier/path, App Sandbox entitlement, and signing identity to macOS Unified
Logging. For this development build, query the startup entry with:

```sh
log show --last 10m --style compact --predicate 'subsystem == "local.desktop-assistant" AND category == "startup"'
```

The development launcher uses an ad-hoc signature with a stable designated
requirement based on the bundle identifier. This keeps macOS TCC consent tied
to the app identity rather than to a different executable hash after each
rebuild. A stable Developer ID signing certificate is not installed on this
Mac; release distribution should use a real Developer ID identity.

The current app bundle identifier is `local.desktop-assistant`. To clear only
its Accessibility consent record (this revokes that permission; macOS will ask
again), run:

```sh
tccutil reset Accessibility local.desktop-assistant
```

To select another installed vision-capable model, set `OLLAMA_VISION_MODEL`,
for example:

```sh
OLLAMA_VISION_MODEL=llava python3 assistant.py
```

## Tests

```sh
python3 -m unittest discover -s tests
```

## Current limitations

This is an early desktop-controller prototype, not a fully reliable
computer-use agent. It can misread the screen; autonomous actions may therefore
do the wrong thing. It only supports visible mouse/keyboard interaction, not
direct file-system access or app-specific integrations. Web research is
optional and searches the task text in your browser. The menu-bar interface is a
development build launched from the project folder.
