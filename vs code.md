# Helm — My Changes & Next Steps

> **Purpose:** A straightforward record of what I changed, what those changes
> do, and what I recommend adding next.
>
> **Status:** Suggestions are future ideas—not features that have already been
> implemented.

## At a glance

| Area | What I changed |
| --- | --- |
| Telegram | Simple “Open Discord” or “Open Safari” requests now launch the app directly. |
| Recovery | Desktop and browser tasks save progress and check uncertain actions before retrying. |
| Browser | Duplicate matching controls no longer trigger an automatic first-match click. |
| Welcome | First-run setup includes a practical quick-start tutorial. |
| Launching | Added a Finder launcher and Terminal commands to open or quit Helm. |
| Earlier work | Added specialist agents, planning, source capture, skills, and draft dictation. |

## Changes I made

### 1. Fixed simple app launches from Telegram

Telegram had replied “Opening Discord,” but the model had only written a
message—it had not called the app-launch tool. That meant macOS never received
a request to open Discord.

I updated [assistant.py](./assistant.py) to recognize simple requests for
supported apps and send them directly to macOS's app launcher. Helm reports the
actual result, respects the disabled-tools setting, and reports launch errors
instead of claiming success.

**Related files:** [assistant.py](./assistant.py),
[tests/test_assistant.py](./tests/test_assistant.py),
[README.md](./README.md)

### 2. Made interrupted tasks safer to resume

I updated [assistant.py](./assistant.py) to save completed task steps and the
action about to run. Saved state is written atomically with private file
permissions. When an interruption leaves it unclear whether an action happened,
Helm is instructed to inspect the current state before retrying—and not repeat
a side effect it cannot verify. Browser page-restore failures are reported
instead of silently ignored.

I also updated workflow recovery in
[ChatView.swift](./Sources/MacMenuAssistant/ChatView.swift) so a resumed agent
checks its saved conversation and current state before continuing, especially
before sends, submissions, purchases, or deletions.

### 3. Prevented ambiguous browser actions

I updated [browser_automation.py](./browser_automation.py) so a click or text
entry proceeds only if the requested accessible control matches exactly one
element. If a page has duplicate controls with the same name, Helm stops rather
than silently choosing the first.

**Related tests:** [tests/test_browser_automation.py](./tests/test_browser_automation.py)

### 4. Added a first-run tutorial

I added a **Quick Start** section to the setup page in
[ChatView.swift](./Sources/MacMenuAssistant/ChatView.swift). It explains how
to choose a model and abilities, enter or dictate a task, review approvals, try
a sample request, and reopen Helm later.

### 5. Added launch shortcuts

- [Open Helm.command](./Open%20Helm.command) launches Helm when double-clicked
  in Finder.
- I added `open helm bot` and `quit helm bot` to my local `~/.zshrc`. They
  launch and quit Helm from Terminal. Open a new Terminal window, or run
  `source ~/.zshrc`, to load the commands.

### Earlier project improvements

Earlier in this project, I also worked on:

- **Three specialist agents:** Researcher, Operator, and Verifier, with plans
  and saved workflow checkpoints.
- **Research and reusable guidance:** source capture and scoped, user-authored
  `SKILL.md` files.
- **Dictation:** speech can be transcribed into the draft for review before
  sending.

These are broader existing project improvements, not all part of the Telegram
launch fix.

## What I recommend adding next

These are recommendations; I have **not** implemented them.

1. **Verify outcomes.** Confirm the visible app or page state after opening an
   app, clicking a control, or changing a field before reporting success.
2. **Make approvals specific.** Show the exact recipient and message, site and
   submitted content, or target and consequence. An approval should apply only
   to the action the user reviewed.
3. **Add a clear task timeline.** Show whether each step is proposed, approved,
   performed, verified, failed, or uncertain. Keep diagnostic data local and
   avoid saving screenshots, page contents, or secrets by default.
4. **Test recovery edge cases.** Cover interruption immediately before and
   after an action, declined approvals, changed app/browser state, duplicate
   actions, and actions whose result was not saved.
5. **Add skill-management controls.** Let users review, edit, disable, and
   delete saved guidance. Skills should never grant permissions or bypass
   approval rules.
6. **Improve integration status.** Show whether each bridge is running and
   explain errors without exposing bot tokens or other credentials.

## Validation

After the Telegram launch change, **169 Python tests passed** and the native
macOS app built successfully. The build still reports existing Swift compiler
warnings.
