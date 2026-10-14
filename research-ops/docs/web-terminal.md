# Web Terminal

`/terminal` provides one interactive shell per Orbitree runtime. It is a
terminal, not an AI conversation: input and output do not enter Results, Memory,
Goals, notifications, or model prompts. Normal shell history policies still
apply. Avoid putting credentials directly into commands.

## Enable

Install `tmux` using the operating system package manager, then configure:

```dotenv
ROPS_WEB_TERMINAL_ENABLED=true
ROPS_WEB_TERMINAL_CWD=/absolute/path/to/workspace
ROPS_WEB_TERMINAL_SHELL=/bin/bash
```

Build the frontend and restart Orbitree when conversations are idle. Keep
`ROPS_INFOFLOW_PUBLIC_BASE_URL` set to the browser's HTTPS origin; additional
origins must be explicitly listed in `ROPS_ALLOWED_ORIGINS`. The reverse proxy
must support WebSocket upgrades for `/api/ws/terminal`.

On a single-user deployment, run this in the server's local terminal:

```bash
/path/to/research-ops/.venv/bin/rops terminal key
```

Enter the returned key on the Web Terminal page. It stays in that browser tab's
session storage, not in the URL. The key file is private (`0600`) under
`<runtime>/web-terminal/unlock.key`; do not publish or commit it. A backend
configured with an access token uses the existing authenticated API instead.

Team mode uses the signed-in account and its private worker token. New team
workers enable the terminal by default. Existing workers must explicitly enable
it in their settings. The shell retains the worker's Linux UID, private home,
and filesystem restrictions. It does not impersonate another user's Claude or
Codex login. A root single-user server gives the terminal root privileges.

## Session Behavior

- Opening an unlocked terminal starts or reattaches to the single shell.
- Leaving the page, refreshing, or restarting the API only detaches the browser.
  The shell and its processes stay in a private tmux session.
- Another browser can take control of the same shell. The old browser stops
  accepting input and does not automatically compete to reconnect.
- Ctrl+C interrupts the shell's foreground command. Ending the terminal requires
  confirmation and closes this tmux session. Other Orbitree conversations
  are not stopped. Detached jobs may outlive a shell, as in a local terminal.
- An operating system reboot, killing tmux, or explicit terminal termination
  ends the shell. This is not a durable job scheduler.
- Display history and transport buffers are bounded. If output overwhelms the
  browser, use Ctrl+C and reconnect. There is no permanent terminal transcript.
- Copy uses the browser Clipboard API with a selected-text fallback. With text
  selected, Ctrl+C copies; without a selection it still interrupts the foreground
  process. Ctrl+Shift+C and Cmd+C explicitly copy. TUI applications can capture
  mouse input; hold Shift while dragging to select terminal text in that case.
  The paste button still requires clipboard-read permission. Toolbar actions
  return focus to the terminal unless focus has moved elsewhere.
- Screen-reader mode is an opt-in toolbar preference. Normal input mode supports
  input-only events used by mobile keyboards and some IMEs. Mobile also has
  explicit Escape and Tab controls.

The tmux socket resides in a private per-UID, per-runtime temporary directory.
Only allowlisted environment variables reach the shell; Orbitree model API
keys are not automatically exported. Shell startup files can still load the
user's own configuration. Terminal tickets are one-use, expire after 30 seconds,
and are bound to the browser Origin. An unauthenticated status request never
starts a shell or reveals the unlock key.

## Verification

```bash
.venv/bin/python -m pytest tests/test_web_terminal.py tests/test_team_websocket.py -q
cd web
npm test -- src/views/TerminalView.test.tsx src/components/AppShell.test.tsx
npm run build -- --outDir dist-terminal
```

From the repository root, run `node scripts/check-web-terminal-ui.mjs
web/dist-terminal` for real-shell desktop/mobile browser checks. Set
`PLAYWRIGHT_CHROMIUM_EXECUTABLE` if Chromium is installed outside Playwright's
default cache. The fixture uses temporary files and no production database,
external jobs, or AI runtimes.
