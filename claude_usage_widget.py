#!/usr/bin/env python3
"""Claude usage widget - a small always-on-top desktop meter.

Stdlib only. Windows / macOS / Linux.

    python claude_usage_widget.py            # run the widget
    python claude_usage_widget.py --probe    # print the raw usage JSON and exit
"""

from __future__ import annotations

import json
import os
import platform
import random
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"

# This endpoint is undocumented and publishes no rate limit or suggested polling
# interval, so these numbers are a deliberately conservative guess rather than a
# figure from Anthropic. 5 minutes is far finer than the bars need: a 5-hour
# window can only move ~0.3%/min even at full tilt, and the weekly and monthly
# bars move slower still. Override with CLAUDE_USAGE_POLL_SECONDS if you want.
BASE_POLL_SECONDS = max(60, int(os.environ.get("CLAUDE_USAGE_POLL_SECONDS") or 300))

# On 429 or network error, back off geometrically to this ceiling.
MAX_POLL_SECONDS = 30 * 60

# When readings stop changing you are not burning quota, so there is nothing to
# watch; drift toward this instead of polling at full rate against idle numbers.
IDLE_MAX_SECONDS = 15 * 60

# Spread clients out so many widgets don't land on the same second.
JITTER_FRACTION = 0.1

STALE_AFTER_SECONDS = 60 * 60  # past this, last-known-good stops being shown

STATE_FILE = Path.home() / ".claude-usage-widget.json"

BG = "#141414"
FG = "#e8e6e3"
DIM = "#8a8580"
TRACK = "#2c2a28"
OK = "#6bbf59"
WARN = "#d9a341"
CRIT = "#d9534f"


# --------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------

def _from_env() -> str | None:
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_OAUTH_TOKEN"):
        tok = os.environ.get(var)
        if tok:
            return tok.strip()
    return None


def _from_file() -> str | None:
    path = Path.home() / ".claude" / ".credentials.json"
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _dig_token(blob)


def _from_keychain() -> str | None:
    """macOS stores the credential blob in the login keychain."""
    if platform.system() != "Darwin":
        return None
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return _dig_token(json.loads(out.stdout))
    except ValueError:
        return None


def _dig_token(blob) -> str | None:
    """Find an access token anywhere in a nested credential blob."""
    if isinstance(blob, dict):
        for key in ("accessToken", "access_token"):
            val = blob.get(key)
            if isinstance(val, str) and val:
                return val
        for val in blob.values():
            found = _dig_token(val)
            if found:
                return found
    return None


def get_token() -> str | None:
    return _from_env() or _from_file() or _from_keychain()


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

class RateLimited(Exception):
    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


class AuthFailed(Exception):
    pass


def fetch_usage(token: str) -> dict:
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": OAUTH_BETA,
            "Accept": "application/json",
            "User-Agent": "claude-usage-widget/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        if err.code == 429:
            # Not sent today, but honour it if it ever appears.
            raw = err.headers.get("Retry-After") if err.headers else None
            try:
                after = float(raw) if raw else None
            except ValueError:
                after = None
            raise RateLimited(after) from err
        if err.code in (401, 403):
            raise AuthFailed(f"HTTP {err.code}") from err
        raise RuntimeError(f"HTTP {err.code}") from err


# --------------------------------------------------------------------------
# parsing
#
# The payload carries a canonical `limits` array of window objects, each with a
# `kind`, a `percent`, a `severity` and - for per-model caps - a `scope` naming
# the model. We read that rather than the top-level `five_hour` / `seven_day_*`
# keys: the per-model top-level keys are null in practice, and taking the model
# name from `scope.model.display_name` means the Fable bar keeps working if the
# premium model is ever renamed. Credits come from `spend`.
# --------------------------------------------------------------------------

KIND_LABELS = {
    "session": "5-hour limit",
    "five_hour": "5-hour limit",
    "weekly_all": "Weekly (all models)",
    "seven_day": "Weekly (all models)",
    "weekly_oauth_apps": "Weekly (apps)",
}

# Fallback if a scoped weekly window arrives with no usable model name.
SCOPED_FALLBACK = "Weekly (scoped)"

# Rendering order: 5-hour, weekly all, weekly per-model, credits, anything else.
ORDER = {"session": 0, "weekly_all": 1, "weekly_scoped": 2, "spend": 3}


def _scoped_label(scope) -> str:
    if not isinstance(scope, dict):
        return SCOPED_FALLBACK
    model = scope.get("model")
    if isinstance(model, dict):
        name = model.get("display_name") or model.get("id")
        if name:
            return f"Weekly ({name})"
    surface = scope.get("surface")
    if isinstance(surface, dict):
        name = surface.get("display_name") or surface.get("id")
        if name:
            return f"Weekly ({name})"
    if isinstance(surface, str) and surface:
        return f"Weekly ({surface})"
    return SCOPED_FALLBACK


def _money(node) -> str | None:
    """Render a {amount_minor, currency, exponent} object as a short string."""
    if not isinstance(node, dict):
        return None
    minor, exponent = node.get("amount_minor"), node.get("exponent")
    if not isinstance(minor, (int, float)):
        return None
    scaled = minor / (10 ** exponent) if isinstance(exponent, int) else minor
    symbol = "$" if node.get("currency") == "USD" else ""
    return f"{symbol}{scaled:,.2f}"


def _spend_window(payload: dict) -> dict | None:
    spend = payload.get("spend")
    if not isinstance(spend, dict) or not spend.get("enabled"):
        return None
    pct = spend.get("percent")
    if not isinstance(pct, (int, float)):
        return None
    used, limit = _money(spend.get("used")), _money(spend.get("limit"))
    note = f"{used} of {limit}" if used and limit else ""
    return {
        "key": "spend",
        "label": "Usage credits",
        "percent": max(0.0, min(100.0, float(pct))),
        "resets_at": None,
        "severity": spend.get("severity"),
        "note": note,
    }


def parse_windows(payload: dict) -> list[dict]:
    """Flatten a usage payload into renderable bars."""
    found: list[dict] = []

    for entry in payload.get("limits") or []:
        if not isinstance(entry, dict):
            continue
        pct = entry.get("percent")
        if not isinstance(pct, (int, float)):
            continue
        kind = entry.get("kind") or ""
        if kind == "weekly_scoped":
            label = _scoped_label(entry.get("scope"))
        else:
            label = KIND_LABELS.get(kind, kind.replace("_", " ").capitalize())
        found.append({
            "key": kind,
            "label": label,
            "percent": max(0.0, min(100.0, float(pct))),
            "resets_at": entry.get("resets_at"),
            "severity": entry.get("severity"),
            "note": "",
        })

    spend = _spend_window(payload)
    if spend:
        found.append(spend)

    # Legacy fallback: if `limits` was absent, read the flat top-level windows.
    if not found:
        for key, label in (("five_hour", "5-hour limit"),
                           ("seven_day", "Weekly (all models)")):
            node = payload.get(key)
            if isinstance(node, dict) and isinstance(
                node.get("utilization"), (int, float)
            ):
                found.append({
                    "key": key,
                    "label": label,
                    "percent": max(0.0, min(100.0, float(node["utilization"]))),
                    "resets_at": node.get("resets_at"),
                    "severity": None,
                    "note": "",
                })

    # De-duplicate by label, keeping the first occurrence.
    seen: set[str] = set()
    unique = []
    for win in found:
        if win["label"] in seen:
            continue
        seen.add(win["label"])
        unique.append(win)

    unique.sort(key=lambda w: ORDER.get(w["key"], 9))
    return unique


def humanize_reset(iso: str | None) -> str:
    if not iso:
        return ""
    text = iso.replace("Z", "+00:00")
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = (when - datetime.now(timezone.utc)).total_seconds()
    if delta <= 0:
        return "resets now"
    hours, minutes = divmod(int(delta // 60), 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"resets in {days}d {hours}h"
    if hours:
        return f"resets in {hours}h {minutes}m"
    return f"resets in {minutes}m"


SHORT_LABELS = {"session": "5h", "weekly_all": "Week", "spend": "Credits"}


def tooltip_text(windows: list[dict], stale: bool) -> str:
    """One-line summary for the tray hover. Windows caps tooltips at 127 chars."""
    parts = []
    for win in windows:
        name = SHORT_LABELS.get(win["key"])
        if name is None:
            label = win["label"]
            # "Weekly (Fable)" -> "Fable"
            name = label[label.find("(") + 1:label.rfind(")")] if "(" in label else label
        parts.append(f"{name} {win['percent']:.0f}%")
    summary = "  ".join(parts)
    prefix = "Claude usage (stale)" if stale else "Claude usage"
    return f"{prefix}\n{summary}"[:127]


SEVERITY_COLORS = {"critical": CRIT, "warning": WARN, "warn": WARN, "normal": OK}


def bar_color(pct: float, severity: str | None = None) -> str:
    # Prefer the server's own severity call; fall back to thresholds.
    if isinstance(severity, str):
        known = SEVERITY_COLORS.get(severity.lower())
        if known:
            return known
    if pct >= 90:
        return CRIT
    if pct >= 70:
        return WARN
    return OK


# --------------------------------------------------------------------------
# poller
# --------------------------------------------------------------------------

class Poller(threading.Thread):
    """Background fetch loop with geometric backoff on 429."""

    daemon = True

    def __init__(self, on_update) -> None:
        super().__init__()
        self.on_update = on_update
        self.interval = BASE_POLL_SECONDS
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.last_good: list[dict] | None = None
        self.last_good_at: float = 0.0

    def refresh_now(self) -> None:
        self.interval = BASE_POLL_SECONDS
        self._wake.set()

    def _sleep_for(self) -> float:
        """Jitter the wait so many clients don't sync up on one second."""
        spread = self.interval * JITTER_FRACTION
        return max(30.0, self.interval + random.uniform(-spread, spread))

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def run(self) -> None:
        while not self._stop.is_set():
            self._tick()
            self._wake.wait(self._sleep_for())
            self._wake.clear()

    def _tick(self) -> None:
        token = get_token()
        if not token:
            self.on_update({"error": "Not signed in - run `claude` once to authenticate"})
            self.interval = MAX_POLL_SECONDS
            return
        try:
            windows = parse_windows(fetch_usage(token))
        except RateLimited as err:
            if err.retry_after:
                self.interval = min(max(err.retry_after, BASE_POLL_SECONDS),
                                    MAX_POLL_SECONDS)
            else:
                self.interval = min(self.interval * 2, MAX_POLL_SECONDS)
            self.on_update(self._stale_payload("rate limited"))
            return
        except AuthFailed as err:
            self.on_update({"error": f"Auth failed ({err}) - re-run `claude`"})
            self.interval = MAX_POLL_SECONDS
            return
        except Exception as err:  # network hiccup, malformed body, etc.
            self.interval = min(self.interval * 2, MAX_POLL_SECONDS)
            self.on_update(self._stale_payload(str(err) or "fetch failed"))
            return

        if not windows:
            self.on_update({"error": "No limit data in response - try --probe"})
            return

        # Idle backoff: identical percentages mean no quota is being spent, so
        # ease off. Any movement snaps straight back to the base interval.
        changed = self.last_good is None or [
            (w["label"], round(w["percent"], 1)) for w in windows
        ] != [(w["label"], round(w["percent"], 1)) for w in self.last_good]
        if changed:
            self.interval = BASE_POLL_SECONDS
        else:
            # max() so a base interval above the idle ceiling never speeds up.
            self.interval = max(BASE_POLL_SECONDS,
                                min(self.interval * 1.5, IDLE_MAX_SECONDS))

        self.last_good = windows
        self.last_good_at = time.time()
        self.on_update({"windows": windows, "stale": False, "at": self.last_good_at})

    def _stale_payload(self, reason: str) -> dict:
        fresh_enough = (
            self.last_good is not None
            and time.time() - self.last_good_at < STALE_AFTER_SECONDS
        )
        if fresh_enough:
            return {
                "windows": self.last_good,
                "stale": True,
                "at": self.last_good_at,
                "note": reason,
            }
        return {"error": reason}


# --------------------------------------------------------------------------
# widget
# --------------------------------------------------------------------------

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


class Widget:
    WIDTH = 288

    def __init__(self, tk, root) -> None:
        self.tk = tk
        self.root = root
        self.state = load_state()
        self.rows: dict[str, dict] = {}
        self._last: dict | None = None
        self.pinned = bool(self.state.get("pinned", True))

        root.title("Claude usage")
        root.configure(bg=BG)
        root.overrideredirect(True)
        root.attributes("-topmost", self.pinned)
        geom = self.state.get("geometry")
        root.geometry(geom if geom else "+80+80")

        self.frame = tk.Frame(root, bg=BG, highlightthickness=1,
                              highlightbackground="#333")
        self.frame.pack(fill="both", expand=True)

        header = tk.Frame(self.frame, bg=BG)
        header.pack(fill="x", padx=12, pady=(10, 2))
        title = tk.Label(header, text="Claude usage", bg=BG, fg=FG,
                         font=("Segoe UI", 10, "bold"))
        title.pack(side="left")

        # Packed right-to-left, so close ends up rightmost.
        close = tk.Label(header, text="✕", bg=BG, fg=DIM,
                         font=("Segoe UI", 9), cursor="hand2")
        close.pack(side="right", padx=(8, 0))
        close.bind("<Button-1>", lambda _e: self.quit())
        self._hover(close, CRIT)

        self.min_button = tk.Label(header, text="–", bg=BG, fg=DIM,
                                   font=("Segoe UI", 11), cursor="hand2")
        self.min_button.pack(side="right")
        self.min_button.bind("<Button-1>", lambda _e: self.minimize())
        self._hover(self.min_button, FG)

        self.body = tk.Frame(self.frame, bg=BG)
        self.body.pack(fill="both", expand=True, padx=12, pady=(4, 2))

        self.status = tk.Label(self.frame, text="loading…", bg=BG, fg=DIM,
                               font=("Segoe UI", 8), anchor="w")
        self.status.pack(fill="x", padx=12, pady=(2, 9))

        for target in (self.frame, header, title, self.body, self.status):
            target.bind("<Button-1>", self._drag_start)
            target.bind("<B1-Motion>", self._drag_move)
            target.bind("<Button-3>", self._menu)

        self.poller = Poller(self._on_update)
        self.poller.start()

        # Tray must be up before minimize is offered, or hiding the window would
        # leave no way to bring it back. If it fails we collapse instead.
        self.collapsed = False
        self.tray = WindowsTray(
            default_icon_path(), "Claude usage",
            self._tray_restore, self._tray_refresh, self._tray_quit,
        )
        self.tray_ok = self.tray.start()

        self._retick()

    # -- minimize / restore ----------------------------------------------
    def _hover(self, widget, color: str) -> None:
        widget.bind("<Enter>", lambda _e: widget.config(fg=color), add="+")
        widget.bind("<Leave>", lambda _e: widget.config(fg=DIM), add="+")

    def minimize(self) -> None:
        if self.tray_ok:
            self.root.withdraw()
        else:
            # No tray backend: collapse to the title bar instead of hiding, so
            # the widget is always recoverable.
            self._collapse(not self.collapsed)

    def restore(self) -> None:
        if self.collapsed:
            self._collapse(False)
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", self.pinned)

    def _collapse(self, on: bool) -> None:
        self.collapsed = on
        if on:
            self.body.pack_forget()
            self.status.pack_forget()
            self.min_button.config(text="+", font=("Segoe UI", 9))
        else:
            self.body.pack(fill="both", expand=True, padx=12, pady=(4, 2))
            self.status.pack(fill="x", padx=12, pady=(2, 9))
            self.min_button.config(text="–", font=("Segoe UI", 11))

    # Tray callbacks arrive on the tray thread; hop onto the tk loop.
    def _tray_restore(self) -> None:
        self.root.after(0, self.restore)

    def _tray_refresh(self) -> None:
        self.poller.refresh_now()

    def _tray_quit(self) -> None:
        self.root.after(0, self.quit)

    # -- interaction ------------------------------------------------------
    def _drag_start(self, event) -> None:
        self._ox, self._oy = event.x_root, event.y_root
        self._wx, self._wy = self.root.winfo_x(), self.root.winfo_y()

    def _drag_move(self, event) -> None:
        x = self._wx + (event.x_root - self._ox)
        y = self._wy + (event.y_root - self._oy)
        self.root.geometry(f"+{x}+{y}")

    def _menu(self, event) -> None:
        menu = self.tk.Menu(self.root, tearoff=0, bg=BG, fg=FG,
                            activebackground="#333", activeforeground=FG,
                            borderwidth=0)
        menu.add_command(label="Refresh now", command=self.poller.refresh_now)
        menu.add_command(
            label="Minimize to tray" if self.tray_ok else "Collapse",
            command=self.minimize,
        )
        menu.add_checkbutton(label="Always on top", command=self.toggle_pin,
                             onvalue=1, offvalue=0,
                             variable=self.tk.IntVar(value=int(self.pinned)))
        menu.add_separator()
        menu.add_command(label="Quit", command=self.quit)
        menu.tk_popup(event.x_root, event.y_root)

    def toggle_pin(self) -> None:
        self.pinned = not self.pinned
        self.root.attributes("-topmost", self.pinned)

    def quit(self) -> None:
        self.poller.stop()
        self.tray.stop()
        save_state({
            "geometry": f"+{self.root.winfo_x()}+{self.root.winfo_y()}",
            "pinned": self.pinned,
        })
        self.root.destroy()

    # -- rendering --------------------------------------------------------
    def _on_update(self, payload: dict) -> None:
        # Poller runs off-thread; hop back onto the tk loop.
        self.root.after(0, lambda: self._render(payload))

    def _render(self, payload: dict) -> None:
        self._last = payload
        if "error" in payload:
            self._clear_rows()
            self.status.config(text=payload["error"], fg=CRIT)
            self.tray.set_tooltip(f"Claude usage\n{payload['error']}"[:127])
            return

        for win in payload["windows"]:
            self._row(win)

        # Drop rows the API stopped reporting.
        live = {w["label"] for w in payload["windows"]}
        for label in [k for k in self.rows if k not in live]:
            self.rows.pop(label)["frame"].destroy()

        self.tray.set_tooltip(
            tooltip_text(payload["windows"], bool(payload.get("stale")))
        )

        when = time.strftime("%H:%M", time.localtime(payload["at"]))
        if payload.get("stale"):
            self.status.config(
                text=f"last known {when} · {payload.get('note', 'stale')}",
                fg=WARN,
            )
        else:
            self.status.config(text=f"updated {when}", fg=DIM)

    def _row(self, win: dict) -> None:
        label = win["label"]
        row = self.rows.get(label)
        if row is None:
            frame = self.tk.Frame(self.body, bg=BG)
            frame.pack(fill="x", pady=(0, 7))
            top = self.tk.Frame(frame, bg=BG)
            top.pack(fill="x")
            name = self.tk.Label(top, text=label, bg=BG, fg=FG,
                                 font=("Segoe UI", 8), anchor="w")
            name.pack(side="left")
            pct = self.tk.Label(top, text="", bg=BG, fg=FG,
                                font=("Segoe UI", 8, "bold"), anchor="e")
            pct.pack(side="right")
            track = self.tk.Frame(frame, bg=TRACK, height=6)
            track.pack(fill="x", pady=(3, 1))
            track.pack_propagate(False)
            fill = self.tk.Frame(track, bg=OK, height=6)
            fill.place(x=0, y=0, relwidth=0.0, height=6)
            reset = self.tk.Label(frame, text="", bg=BG, fg=DIM,
                                  font=("Segoe UI", 7), anchor="w")
            reset.pack(fill="x")
            row = {"frame": frame, "pct": pct, "fill": fill, "reset": reset}
            self.rows[label] = row
            for target in (frame, top, name, pct, track, reset):
                target.bind("<Button-1>", self._drag_start)
                target.bind("<B1-Motion>", self._drag_move)
                target.bind("<Button-3>", self._menu)

        value = win["percent"]
        row["pct"].config(text=f"{value:.0f}%")
        row["fill"].config(bg=bar_color(value, win.get("severity")))
        row["fill"].place_configure(relwidth=value / 100.0)
        row["reset"].config(
            text=win.get("note") or humanize_reset(win.get("resets_at"))
        )

    def _clear_rows(self) -> None:
        for row in self.rows.values():
            row["frame"].destroy()
        self.rows.clear()

    def _retick(self) -> None:
        # Reset countdowns drift between fetches; re-render them on their own
        # cadence so "resets in 42m" stays honest.
        if self._last:
            self._render(self._last)
        self.root.after(30_000, self._retick)


# --------------------------------------------------------------------------
# icon generation
#
# The installer needs an .ico for the shortcuts. Rather than committing a binary
# blob we draw one here: a dark rounded tile with three meter bars, rendered at
# several sizes and packed into a multi-resolution ICO. Stdlib only, so this
# works on a bare Python with no imaging library.
# --------------------------------------------------------------------------

ICON_SIZES = (16, 32, 48, 64, 128, 256)

# Bars are (vertical centre, fill fraction, colour) in unit space.
ICON_BARS = ((0.31, 0.42, OK), (0.50, 0.72, WARN), (0.69, 1.00, CRIT))


def _bgra(hex_color: str) -> tuple[int, int, int]:
    raw = hex_color.lstrip("#")
    r, g, b = (int(raw[i:i + 2], 16) for i in (0, 2, 4))
    return b, g, r


def _icon_sample(u: float, v: float) -> tuple[int, int, int, int]:
    """Colour at unit coordinate (u, v). Returns BGRA, alpha 0 outside the tile."""
    inset, radius = 0.03, 0.22
    lo, hi = inset, 1.0 - inset
    if not (lo <= u <= hi and lo <= v <= hi):
        return (0, 0, 0, 0)
    # Round the corners: outside the corner arc is transparent.
    cx = lo + radius if u < lo + radius else (hi - radius if u > hi - radius else u)
    cy = lo + radius if v < lo + radius else (hi - radius if v > hi - radius else v)
    if (u - cx) ** 2 + (v - cy) ** 2 > radius ** 2:
        return (0, 0, 0, 0)

    b, g, r = _bgra(BG)
    bar_x0, bar_x1, half = 0.17, 0.83, 0.038
    for centre, fraction, color in ICON_BARS:
        if abs(v - centre) <= half and bar_x0 <= u <= bar_x1:
            filled = u <= bar_x0 + (bar_x1 - bar_x0) * fraction
            b, g, r = _bgra(color if filled else TRACK)
            break
    return (b, g, r, 255)


def _icon_bitmap(size: int) -> bytes:
    """Supersampled BGRA rows, bottom-up as the BMP format wants."""
    ss = 4 if size <= 64 else 2
    step = 1.0 / (size * ss)
    rows: list[bytes] = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            acc_b = acc_g = acc_r = acc_a = 0
            for sy in range(ss):
                v = (y * ss + sy + 0.5) * step
                for sx in range(ss):
                    u = (x * ss + sx + 0.5) * step
                    sb, sg, sr, sa = _icon_sample(u, v)
                    # Weight colour by alpha so transparent edges don't darken.
                    acc_b += sb * sa
                    acc_g += sg * sa
                    acc_r += sr * sa
                    acc_a += sa
            if acc_a:
                row += bytes((acc_b // acc_a, acc_g // acc_a, acc_r // acc_a,
                              acc_a // (ss * ss)))
            else:
                row += b"\x00\x00\x00\x00"
        rows.append(bytes(row))
    return b"".join(reversed(rows))


def write_icon(path: Path) -> None:
    import struct

    images: list[bytes] = []
    for size in ICON_SIZES:
        bgra = _icon_bitmap(size)
        # 32bpp images still need an AND mask; all-zero means "use the alpha".
        mask_stride = ((size + 31) // 32) * 4
        mask = b"\x00" * (mask_stride * size)
        header = struct.pack(
            "<IiiHHIIiiII",
            40, size, size * 2, 1, 32, 0, len(bgra) + len(mask), 0, 0, 0, 0,
        )
        images.append(header + bgra + mask)

    offset = 6 + 16 * len(images)
    directory = b""
    for size, blob in zip(ICON_SIZES, images):
        directory += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,  # 256 is encoded as 0
            0 if size >= 256 else size,
            0, 0, 1, 32, len(blob), offset,
        )
        offset += len(blob)

    path.write_bytes(
        struct.pack("<HHH", 0, 1, len(images)) + directory + b"".join(images)
    )


def default_icon_path() -> Path | None:
    """The installed icon if present, otherwise a cached generated one."""
    beside = Path(__file__).with_name("widget.ico")
    if beside.exists():
        return beside
    cached = Path.home() / ".claude-usage-widget.ico"
    if cached.exists():
        return cached
    try:
        write_icon(cached)
        return cached
    except Exception:
        return None


# --------------------------------------------------------------------------
# system tray (Windows)
#
# tkinter has no tray support on any platform, so this talks to Shell_NotifyIcon
# through ctypes. The tray icon owns a hidden message-only window, and a window
# and its message queue belong to the thread that created them - so we create it
# on a dedicated thread running its own GetMessage loop, and hop back to the
# tkinter thread via root.after() for anything that touches the UI.
# --------------------------------------------------------------------------

WM_APP = 0x8000
WM_TRAY_CALLBACK = WM_APP + 1
WM_TRAY_SETTIP = WM_APP + 2
WM_TRAY_STOP = WM_APP + 3

WM_DESTROY = 0x0002
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

MENU_SHOW, MENU_REFRESH, MENU_QUIT = 1, 2, 3


class WindowsTray:
    """A Shell_NotifyIcon tray entry driven from its own thread."""

    def __init__(self, icon_path: Path | None, tooltip: str,
                 on_restore, on_refresh, on_quit) -> None:
        self.icon_path = icon_path
        self.tooltip = tooltip
        self.on_restore = on_restore
        self.on_refresh = on_refresh
        self.on_quit = on_quit
        self.hwnd = None
        self.available = False
        self._ready = threading.Event()
        self._tip_lock = threading.Lock()
        self._pending_tip = tooltip
        self._thread: threading.Thread | None = None

    # -- public -----------------------------------------------------------
    def start(self) -> bool:
        if platform.system() != "Windows":
            return False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # If the tray can't be created we must know before offering to hide the
        # window, or the user ends up with no way to get it back.
        self._ready.wait(timeout=5)
        return self.available

    def set_tooltip(self, text: str) -> None:
        if not self.available or not self.hwnd:
            return
        with self._tip_lock:
            self._pending_tip = text[:127]
        try:
            self._user32.PostMessageW(self.hwnd, WM_TRAY_SETTIP, 0, 0)
        except Exception:
            pass

    def stop(self) -> None:
        if self.available and self.hwnd:
            try:
                self._user32.PostMessageW(self.hwnd, WM_TRAY_STOP, 0, 0)
            except Exception:
                pass

    # -- thread body ------------------------------------------------------
    def _run(self) -> None:
        try:
            self._setup_win32()
        except Exception:
            self.available = False
            self._ready.set()
            return
        self._ready.set()
        self._pump()

    def _setup_win32(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._shell32 = shell32

        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(
            LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        )

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("uVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_byte * 16),
                ("hBalloonIcon", wintypes.HICON),
            ]

        self._NOTIFYICONDATAW = NOTIFYICONDATAW

        # Declare signatures explicitly. Without this ctypes guesses from the
        # Python values, and an unhandled message carrying a wide LPARAM
        # overflows on the way into DefWindowProcW.
        self._user32.DefWindowProcW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        ]
        self._user32.DefWindowProcW.restype = LRESULT
        self._user32.PostMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        ]
        self._user32.PostMessageW.restype = wintypes.BOOL
        self._user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT
        ]
        self._user32.GetMessageW.restype = ctypes.c_int

        # Keep a reference or ctypes will collect the callback mid-flight.
        self._wndproc = WNDPROC(self._on_message)

        hinstance = kernel32.GetModuleHandleW(None)
        # Unique class name per process; re-registering the same name fails.
        class_name = f"ClaudeUsageWidgetTray{os.getpid()}"
        wclass = WNDCLASSW()
        wclass.lpfnWndProc = self._wndproc
        wclass.hInstance = hinstance
        wclass.lpszClassName = class_name
        if not self._user32.RegisterClassW(ctypes.byref(wclass)):
            raise OSError("RegisterClassW failed")

        self._user32.CreateWindowExW.restype = wintypes.HWND
        self.hwnd = self._user32.CreateWindowExW(
            0, class_name, "Claude usage widget", 0, 0, 0, 0, 0,
            None, None, hinstance, None,
        )
        if not self.hwnd:
            raise OSError("CreateWindowExW failed")

        self._hicon = self._load_icon()

        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self.hwnd
        data.uID = 1
        data.uFlags = 0x01 | 0x02 | 0x04  # MESSAGE | ICON | TIP
        data.uCallbackMessage = WM_TRAY_CALLBACK
        data.hIcon = self._hicon
        data.szTip = self.tooltip[:127]
        self._data = data
        if not shell32.Shell_NotifyIconW(0x00, ctypes.byref(data)):  # NIM_ADD
            raise OSError("Shell_NotifyIconW(NIM_ADD) failed")

        self.available = True

    def _load_icon(self):
        self._user32.LoadImageW.restype = self._wintypes.HANDLE
        self._user32.LoadIconW.restype = self._wintypes.HICON
        if self.icon_path and self.icon_path.exists():
            size = self._user32.GetSystemMetrics(49)  # SM_CXSMICON
            handle = self._user32.LoadImageW(
                None, str(self.icon_path), 1, size, size, 0x00000010,  # LR_LOADFROMFILE
            )
            if handle:
                return handle
        # IDI_APPLICATION as MAKEINTRESOURCE, so a missing .ico still gives a
        # clickable entry rather than an invisible one.
        return self._user32.LoadIconW(None, 32512)

    def _pump(self) -> None:
        ctypes, wintypes = self._ctypes, self._ctypes.wintypes
        msg = wintypes.MSG()
        while self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            self._user32.TranslateMessage(ctypes.byref(msg))
            self._user32.DispatchMessageW(ctypes.byref(msg))

    # -- window procedure -------------------------------------------------
    def _on_message(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY_CALLBACK:
                event = lparam & 0xFFFF
                if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self.on_restore()
                elif event == WM_RBUTTONUP:
                    self._show_menu(hwnd)
                return 0
            if msg == WM_TRAY_SETTIP:
                self._apply_tooltip()
                return 0
            if msg == WM_TRAY_STOP:
                self._remove_icon()
                self._user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                self._user32.PostQuitMessage(0)
                return 0
        except Exception:
            return 0
        return self._user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _apply_tooltip(self) -> None:
        ctypes = self._ctypes
        with self._tip_lock:
            self._data.szTip = self._pending_tip
        self._shell32.Shell_NotifyIconW(0x01, ctypes.byref(self._data))  # NIM_MODIFY

    def _remove_icon(self) -> None:
        ctypes = self._ctypes
        try:
            self._shell32.Shell_NotifyIconW(0x02, ctypes.byref(self._data))  # NIM_DELETE
        except Exception:
            pass
        self.available = False

    def _show_menu(self, hwnd) -> None:
        ctypes = self._ctypes

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        menu = self._user32.CreatePopupMenu()
        if not menu:
            return
        try:
            self._user32.AppendMenuW(menu, 0, MENU_SHOW, "Show widget")
            self._user32.AppendMenuW(menu, 0, MENU_REFRESH, "Refresh now")
            self._user32.AppendMenuW(menu, 0x800, 0, None)  # MF_SEPARATOR
            self._user32.AppendMenuW(menu, 0, MENU_QUIT, "Quit")

            point = POINT()
            self._user32.GetCursorPos(ctypes.byref(point))
            # The tray needs foreground ownership or the menu won't dismiss.
            self._user32.SetForegroundWindow(hwnd)
            choice = self._user32.TrackPopupMenu(
                menu, 0x0100 | 0x0002,  # TPM_RETURNCMD | TPM_RIGHTBUTTON
                point.x, point.y, 0, hwnd, None,
            )
            self._user32.PostMessageW(hwnd, 0x0000, 0, 0)  # WM_NULL, dismiss quirk
        finally:
            self._user32.DestroyMenu(menu)

        if choice == MENU_SHOW:
            self.on_restore()
        elif choice == MENU_REFRESH:
            self.on_refresh()
        elif choice == MENU_QUIT:
            self.on_quit()


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------

def probe() -> int:
    token = get_token()
    if not token:
        print("No OAuth token found. Run `claude` once to sign in, or set "
              "CLAUDE_CODE_OAUTH_TOKEN.", file=sys.stderr)
        return 2
    try:
        payload = fetch_usage(token)
    except RateLimited:
        print("Rate limited (429). Wait a few minutes and retry.", file=sys.stderr)
        return 3
    except Exception as err:
        print(f"Fetch failed: {err}", file=sys.stderr)
        return 1
    print(json.dumps(payload, indent=2))
    print("\n--- parsed as ---")
    for win in parse_windows(payload):
        print(f"{win['label']:<24} {win['percent']:6.1f}%  "
              f"{win.get('resets_at') or '-'}   (key: {win['key']})")
    return 0


def main() -> int:
    if "--probe" in sys.argv:
        return probe()
    if "--make-icon" in sys.argv:
        try:
            target = sys.argv[sys.argv.index("--make-icon") + 1]
        except IndexError:
            print("--make-icon needs an output path", file=sys.stderr)
            return 2
        write_icon(Path(target))
        print(target)
        return 0
    try:
        import tkinter as tk
    except ImportError:
        print("tkinter is not available. On Debian/Ubuntu: sudo apt install "
              "python3-tk", file=sys.stderr)
        return 1
    root = tk.Tk()
    root.minsize(Widget.WIDTH, 1)
    root.maxsize(Widget.WIDTH, 10_000)  # fixed width, content-driven height
    widget = Widget(tk, root)
    root.protocol("WM_DELETE_WINDOW", widget.quit)
    # Useful for launch-at-login: start out of the way in the tray.
    if "--minimized" in sys.argv and widget.tray_ok:
        root.withdraw()
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
