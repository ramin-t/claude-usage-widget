# Claude usage widget

A small always-on-top desktop meter showing your 5-hour limit, weekly all-models
limit, weekly Fable limit and usage credits as bars.

Single file, Python standard library only — no pip install, no Node, no Electron.
Works on Windows, macOS and Linux.

> Unofficial and not affiliated with Anthropic. It reads the same undocumented
> endpoint that backs `/usage` in Claude Code, so it may break without notice.
> See [Polling](#polling) for how it avoids hammering that endpoint.

## Install (Windows)

```powershell
.\install.ps1 -Startup -Launch
```

That copies the widget into `%LOCALAPPDATA%\ClaudeUsageWidget`, generates an
icon, and creates Desktop and Start Menu shortcuts. `-Startup` also adds a
Startup shortcut so it appears at login.

Everything is per-user — no administrator rights, no registry writes, nothing
outside your profile.

| | |
|---|---|
| `.\install.ps1` | install, no autostart |
| `.\install.ps1 -Startup` | add launch-at-login, starting in the tray |
| `.\install.ps1 -Startup -StartupVisible` | launch-at-login, window shown |
| `.\install.ps1 -NoDesktop` | Start Menu shortcut only |
| `.\install.ps1 -Uninstall` | remove shortcuts and files |

Re-running the installer upgrades in place. If PowerShell blocks the script,
either `Unblock-File .\install.ps1` or run it as
`powershell -ExecutionPolicy Bypass -File .\install.ps1`.

The only prerequisite is Python 3.8+ (`winget install Python.Python.3.13`); the
installer checks for it, and for tkinter, before touching anything.

## Install (macOS / Linux)

```bash
./install.sh --startup
```

Linux gets a `.desktop` launcher entry, plus an XDG autostart entry with
`--startup`. macOS gets a LaunchAgent for login start; it has no
Desktop-shortcut concept for scripts, so you launch it by command or wrap it in
an Automator app. `./install.sh --uninstall` reverses either.

Linux needs tkinter, which some distros package separately:
`sudo apt install python3-tk`.

## Run without installing

```bash
python claude_usage_widget.py
```

On Windows, double-click `ClaudeUsageWidget.vbs` to launch with no console
window.

## Using it

- **Drag** anywhere to move it. Position and pin state persist to
  `~/.claude-usage-widget.json`.
- **–** minimizes to the system tray. Click the tray icon to bring it back;
  right-click it for show / refresh / quit. Hovering the tray icon shows a
  summary (`5h 12%  Week 34%  Fable 8%  Credits 0%`) without restoring.
- **Right-click** the widget for refresh / minimize / always-on-top / quit.
- **✕** to close.

With `-Startup`, login launches it straight into the tray. Pass
`-StartupVisible` alongside it if you'd rather see the window at login.

### Tray support by platform

The tray is real on Windows only. tkinter has no tray support on any platform,
so it is implemented directly against `Shell_NotifyIcon` via `ctypes` — no extra
packages. macOS (`NSStatusItem`) and Linux (`StatusNotifierItem` over DBus) have
no stdlib access at all, and doing them properly would mean a dependency such as
`pystray` or `pyobjc`.

So on macOS and Linux the **–** button collapses the widget to just its title
bar instead of hiding it. That is deliberate: hiding a frameless always-on-top
window with no tray to restore it from would make the widget unrecoverable. The
button reports itself as "Collapse" in the right-click menu on those platforms,
and the widget checks that the tray actually initialised before it will ever
hide itself — if tray creation fails on Windows too, you get the same safe
collapse rather than a vanished window.

Bars turn amber then red as you approach a limit, following the `severity` the
API itself reports rather than local thresholds.

## Authentication

It reuses your existing Claude Code login — no separate setup. Credentials are
read, in order, from:

1. `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_OAUTH_TOKEN`
2. `~/.claude/.credentials.json`
3. the macOS login keychain (`Claude Code-credentials`)

If none resolve, the widget says so; run `claude` once to sign in.

The OAuth access token is short-lived (about 8 hours) and Claude Code refreshes
it when it runs. The widget does **not** refresh it — deliberately, since a
refresh can rotate the refresh token and racing Claude Code for that risks
signing you out of both.

Instead it reads the expiry from the credential file and re-checks that file
every 15 seconds, which costs nothing and sends no request. So:

- an expired token is reported as
  `token expired - run \`claude\` to refresh`, without wasting a request on a
  token that cannot work;
- a token the server rejects with 401 is parked rather than resent, so it
  doesn't burn quota retrying something that can't succeed;
- when Claude Code writes a fresh token, the widget notices within ~15 seconds
  and recovers on its own — no restart, and no waiting out a backoff.

This matters most after a reboot: if the widget starts at login before `claude`
has run, the token from yesterday may already be expired.

## Polling

Data comes from `GET https://api.anthropic.com/api/oauth/usage`, the same
undocumented endpoint that backs `/usage` in Claude Code.

**Anthropic publishes no rate limit or suggested interval for it.** The
[platform rate-limit docs](https://platform.claude.com/docs/en/api/rate-limits)
cover the inference APIs, not this one, and the issue asking for a documented
polling interval was closed without providing one
([claude-code#31637](https://github.com/anthropics/claude-code/issues/31637)).
The endpoint also 429s readily and does not send `Retry-After`.

So the defaults below are a conservative guess, not a published figure. The best
available signal is that Claude Code's own `/usage` will show bars up to 60
minutes old rather than re-fetch, which suggests an intended cadence measured in
minutes, on demand.

The widget:

- polls every **5 minutes** while healthy and actively burning quota;
- eases to 15 minutes when readings stop changing, since unchanged percentages
  mean there is nothing to watch — any movement snaps back to 5 minutes;
- doubles the interval on any 429 or network error, to a 30-minute ceiling, and
  honours `Retry-After` if it is ever sent;
- jitters every wait by ±10% so multiple clients don't sync onto one second;
- keeps showing the last good reading through failures, labelled
  `last known HH:MM · rate limited`, and drops it after an hour rather than
  displaying numbers that are quietly stale.

That works out to roughly 12–20 requests/hour under active use and ~4/hour idle.

To change the base rate:

```bash
CLAUDE_USAGE_POLL_SECONDS=600 python claude_usage_widget.py
```

Values below 60s are clamped. "Refresh now" in the right-click menu resets the
backoff, so don't lean on it repeatedly while throttled.

## Troubleshooting the payload

```bash
python claude_usage_widget.py --probe
```

prints the raw JSON plus how it parsed. Useful if a bar goes missing after an
API change.

> Don't paste raw `--probe` output into a public issue. It contains your own
> usage and spend figures, and the response carries field names for things that
> aren't announced. Quote just the `--- parsed as ---` section instead.

## How the bars are derived

The response's `limits[]` array is the source for the three limit bars, keyed on
`kind` (`session`, `weekly_all`, `weekly_scoped`). The Fable bar's label comes
from `scope.model.display_name` rather than a hardcoded string, so it survives
the premium model being renamed. Credits come from the `spend` object, showing
`used of limit` in dollars.

Note that the flat top-level `seven_day_opus` / `seven_day_sonnet` keys are
`null` in practice — they are read only as a fallback if `limits[]` is ever
absent.
