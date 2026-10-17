# Local Desktop Assistant

A local-first macOS assistant prototype. It uses Ollama for local language and
vision models and offers a Terminal interface plus a native menu-bar interface
for task-scoped desktop control.

## Requirements

- macOS
- Python 3.9 or newer
- Xcode Command Line Tools for the menu-bar interface
- [Ollama](https://ollama.com/) with a model downloaded locally
- PyAutoGUI for optional desktop control

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

The launcher compiles the small Swift app with the selected Xcode Command Line
Tools and starts it. The first launch may take a little while to compile.

It appears as a sparkle icon in the menu bar. Enter a task, choose **Ask for
approval** or **Act autonomously**, then press **Start task**. Approval mode
pauses before each proposed mouse or keyboard action. Autonomous mode performs
those actions without per-action prompts for this task only. The setting resets
to approval mode when the task finishes. Use **Stop** to terminate a running
task. Simple requests such as “Open Safari” use macOS's direct app launcher
instead of screen-click automation.

Hold **Control+Option** while speaking, then release the keys to run the
transcribed task autonomously. Speech recognition is configured to run
on-device; if macOS does not support on-device recognition for the selected
language, the app reports that and does not send microphone audio elsewhere.
The first use asks for Microphone and Speech Recognition permissions. The
global shortcut needs Input Monitoring, and macOS may also require
Accessibility. If prompted, grant them to Local Desktop Assistant in System
Settings → Privacy & Security → Input Monitoring / Accessibility, then use
**Retry shortcut** or restart the app.
The menu-bar microphone button is also available for starting and stopping
voice capture manually.

Select **Search the web for examples** to opt in to a browser search (Chrome or
Safari) before a typed or spoken task. The task text is searched on Google and the results are
shown in Activity; only titles, links, and snippets are supplied to the local
vision model. Web results are untrusted reference material, not instructions.
Leave the option off to keep the task query local.

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

## Commands

- Type a message to chat with the local model. Replies are spoken by default.
- `/speak off` disables spoken replies; `/speak on` enables them again.
- Copy text, then use `/read` to speak the clipboard contents. The clipboard is
  only read after you explicitly run this command.
- `/open Safari`, `/open Notes`, `/open Calendar`, `/open Calculator`, or
  `/open Finder` opens one of the allowlisted apps.
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
