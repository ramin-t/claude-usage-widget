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
import urllib.parse
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

# How often to re-read the local credential file. Costs nothing and no request
# is sent, so it can be frequent: this is what makes the widget recover on its
# own seconds after Claude Code refreshes an expired token.
CREDENTIAL_CHECK_SECONDS = 15

# Whether to attempt an OAuth refresh when the token has expired. Disable with
# --no-refresh to leave the credential file strictly read-only.
ALLOW_REFRESH = True

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

# A credential is the token plus, when we can find it, its expiry as a unix
# timestamp. Knowing the expiry matters: it lets us recognise a stale token
# without spending a request, and recover the moment Claude Code refreshes it.
class Credential:
    __slots__ = ("token", "expires_at", "refresh_token", "source", "path",
                 "container", "keys", "persisted")

    def __init__(self, token: str, expires_at: float | None = None,
                 refresh_token: str | None = None, source: str = "env",
                 path: "Path | None" = None,
                 container: tuple[str, ...] = (), keys: dict | None = None) -> None:
        self.token = token
        self.expires_at = expires_at
        self.refresh_token = refresh_token
        self.source = source          # env | file | keychain
        self.path = path              # file we can write a rotated token back to
        self.container = container    # key path to the dict holding the token
        self.keys = keys or {}        # the exact field names found there
        self.persisted = True         # False if a refresh could not be saved

    def refreshable(self) -> bool:
        # Only when we can write a rotated refresh token back. Refreshing without
        # persisting would hand Claude Code a dead credential.
        return bool(self.refresh_token and self.source == "file" and self.path)

    def expired(self, skew: float = 60.0) -> bool:
        """True if the token is past expiry, or close enough to be useless."""
        return self.expires_at is not None and time.time() + skew >= self.expires_at


def _from_env() -> Credential | None:
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_OAUTH_TOKEN"):
        tok = os.environ.get(var)
        if tok:
            # Supplied by hand, so we have no expiry to check.
            return Credential(tok.strip())
    return None


def _from_file() -> Credential | None:
    path = Path.home() / ".claude" / ".credentials.json"
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    found = _dig_credential(blob)
    if found:
        found.source = "file"
        found.path = path
    return found


def _from_keychain() -> Credential | None:
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
        return _dig_credential(json.loads(out.stdout))
    except ValueError:
        return None


def _dig_credential(blob, container: tuple[str, ...] = ()) -> Credential | None:
    """Find an access token, its sibling expiry and refresh token, in a blob.

    Records where it was found and under exactly which field names, so a
    refreshed token can be written back into the same place without assuming a
    particular layout.
    """
    if isinstance(blob, dict):
        for key in ("accessToken", "access_token"):
            val = blob.get(key)
            if isinstance(val, str) and val:
                refresh_key = next(
                    (k for k in ("refreshToken", "refresh_token") if blob.get(k)), None
                )
                expiry_key = next(
                    (k for k in ("expiresAt", "expires_at", "expiry")
                     if isinstance(blob.get(k), (int, float))), None
                )
                raw_expiry = blob.get(expiry_key) if expiry_key else None
                return Credential(
                    val,
                    _expiry_of(blob),
                    blob.get(refresh_key) if refresh_key else None,
                    container=container,
                    keys={
                        "access": key,
                        "refresh": refresh_key,
                        "expiry": expiry_key,
                        # Preserve the unit so we write back what was there.
                        "expiry_ms": bool(raw_expiry and raw_expiry > 1e11),
                    },
                )
        for name, val in blob.items():
            found = _dig_credential(val, container + (name,))
            if found:
                return found
    return None


def _expiry_of(node: dict) -> float | None:
    for key in ("expiresAt", "expires_at", "expiry"):
        val = node.get(key)
        if isinstance(val, (int, float)) and val > 0:
            # Milliseconds if it's implausibly large for seconds.
            return val / 1000.0 if val > 1e11 else float(val)
    return None


def get_credential() -> Credential | None:
    return _from_env() or _from_file() or _from_keychain()


# --------------------------------------------------------------------------
# token refresh
#
# The same flow Claude Code uses. Two things make this delicate:
#
#  - The server may return a NEW refresh token, retiring the one we sent. If we
#    fail to save it, Claude Code is left holding a dead credential. So we only
#    refresh when we can write back, and we report it loudly if the write fails.
#  - We share the file with Claude Code, so the write is atomic (temp +
#    os.replace) and merges into a fresh read rather than overwriting wholesale.
# --------------------------------------------------------------------------

REFRESH_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_CODE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


def _post_token(payload: dict, as_json: bool) -> dict:
    if as_json:
        body, content_type = json.dumps(payload).encode(), "application/json"
    else:
        body = urllib.parse.urlencode(payload).encode()
        content_type = "application/x-www-form-urlencoded"
    req = urllib.request.Request(
        REFRESH_URL, data=body, method="POST",
        headers={
            "Content-Type": content_type,
            "Accept": "application/json",
            "User-Agent": "claude-usage-widget/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def refresh_credential(credential: Credential) -> Credential | None:
    """One refresh attempt. Returns the new credential, or None on failure."""
    if not credential.refreshable():
        return None
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": credential.refresh_token,
        "client_id": CLAUDE_CODE_CLIENT_ID,
    }
    try:
        data = _post_token(payload, as_json=True)
    except urllib.error.HTTPError as err:
        detail = ""
        try:
            detail = err.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        # A complaint about the request shape means the grant was not consumed,
        # so one retry with form encoding is safe. invalid_grant means the token
        # is spent or revoked - retrying would be pointless and confusing.
        if err.code in (400, 415) and "invalid_grant" not in detail:
            try:
                data = _post_token(payload, as_json=False)
            except Exception:
                return None
        else:
            return None
    except Exception:
        return None

    access = data.get("access_token")
    if not isinstance(access, str) or not access:
        return None
    rotated = data.get("refresh_token")
    new_refresh = rotated if isinstance(rotated, str) and rotated else credential.refresh_token
    lifetime = data.get("expires_in")
    expires_at = (
        time.time() + float(lifetime) if isinstance(lifetime, (int, float)) else None
    )

    fresh = Credential(access, expires_at, new_refresh, "file", credential.path,
                       credential.container, dict(credential.keys))
    fresh.persisted = _write_back(fresh)
    return fresh


def _write_back(credential: Credential) -> bool:
    """Merge the new token into the credential file atomically."""
    path = credential.path
    if path is None:
        return False
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False

    node = blob
    for key in credential.container:
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]
    if not isinstance(node, dict):
        return False

    keys = credential.keys
    node[keys.get("access") or "accessToken"] = credential.token
    if keys.get("refresh") and credential.refresh_token:
        node[keys["refresh"]] = credential.refresh_token
    if credential.expires_at is not None:
        field = keys.get("expiry") or "expiresAt"
        node[field] = (
            int(credential.expires_at * 1000) if keys.get("expiry_ms")
            else int(credential.expires_at)
        )

    # Atomic: a crash mid-write must not leave a truncated credential file.
    temp = path.with_name(path.name + ".widget-tmp")
    try:
        temp.write_text(json.dumps(blob, indent=2), encoding="utf-8")
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass  # Windows ignores POSIX modes
        os.replace(temp, path)
        return True
    except OSError:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


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
    """Credential watcher plus a rate-limited fetch loop.

    These are deliberately separate cadences. Checking the credential file is
    free, so we do it often; hitting the network is not, so it stays on the
    interval. That way an expired token is spotted without spending a request,
    and the widget recovers within seconds of Claude Code refreshing it instead
    of sitting in a backoff it can't see out of.
    """

    daemon = True

    def __init__(self, on_update) -> None:
        super().__init__()
        self.on_update = on_update
        self.interval = BASE_POLL_SECONDS
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.last_good: list[dict] | None = None
        self.last_good_at: float = 0.0
        self._next_fetch = 0.0
        self._seen_token: str | None = None
        self._rejected_token: str | None = None
        # Refresh tokens we have already spent an attempt on. One try each, so a
        # revoked token doesn't turn into a retry loop against the auth server.
        self._refresh_attempted: set[str] = set()
        self._save_failed = False

    def refresh_now(self) -> None:
        self.interval = BASE_POLL_SECONDS
        self._next_fetch = 0.0
        self._rejected_token = None  # let a previously-401'd token be retried
        self._wake.set()

    def _sleep_for(self) -> float:
        """Jitter the wait so many clients don't sync up on one second."""
        spread = CREDENTIAL_CHECK_SECONDS * JITTER_FRACTION
        return CREDENTIAL_CHECK_SECONDS + random.uniform(-spread, spread)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def run(self) -> None:
        while not self._stop.is_set():
            self._tick()
            self._wake.wait(self._sleep_for())
            self._wake.clear()

    def _tick(self) -> None:
        credential = get_credential()
        if credential is None:
            self.on_update({"error": "Not signed in - run `claude` once to authenticate"})
            return

        # A new token means a refresh happened: retry at once, even mid-backoff.
        if credential.token != self._seen_token:
            self._seen_token = credential.token
            self._rejected_token = None
            self._next_fetch = 0.0

        if credential.expired():
            renewed = self._try_refresh(credential)
            if renewed is None:
                self.on_update(self._stale_payload(
                    "token expired - run `claude` to refresh"
                ))
                return
            credential = renewed

        # Don't spend requests re-sending a token the server already rejected;
        # wait for it to change. refresh_now() clears this to force a retry.
        if credential.token == self._rejected_token:
            self.on_update(self._stale_payload(
                "auth failed - run `claude` to refresh"
            ))
            return

        if time.time() < self._next_fetch:
            return

        self._fetch(credential)

    def _try_refresh(self, credential: Credential) -> Credential | None:
        """A single refresh attempt per refresh token."""
        if not ALLOW_REFRESH or not credential.refreshable():
            return None
        token = credential.refresh_token or ""
        if token in self._refresh_attempted:
            return None
        self._refresh_attempted.add(token)

        renewed = refresh_credential(credential)
        if renewed is None:
            return None

        self._seen_token = renewed.token
        self._rejected_token = None
        self._next_fetch = 0.0
        if not renewed.persisted:
            # The token works for us, but Claude Code may now hold a retired
            # refresh token. Say so rather than let it fail silently later.
            self._save_failed = True
        return renewed

    def _fetch(self, credential: Credential) -> None:
        token = credential.token
        try:
            windows = parse_windows(fetch_usage(token))
        except RateLimited as err:
            if err.retry_after:
                self.interval = min(max(err.retry_after, BASE_POLL_SECONDS),
                                    MAX_POLL_SECONDS)
            else:
                self.interval = min(self.interval * 2, MAX_POLL_SECONDS)
            self._schedule_next()
            self.on_update(self._stale_payload("rate limited"))
            return
        except AuthFailed:
            # A 401 on a token that looked valid means the expiry was wrong or
            # the token was revoked server-side, so try a refresh here too.
            renewed = self._try_refresh(credential)
            if renewed is not None:
                self._fetch(renewed)
                return
            # Park this token rather than backing off blindly. The credential
            # check above will notice the moment a fresh one is written.
            self._rejected_token = token
            self.on_update(self._stale_payload(
                "auth failed - run `claude` to refresh"
            ))
            return
        except Exception as err:  # network hiccup, malformed body, etc.
            self.interval = min(self.interval * 2, MAX_POLL_SECONDS)
            self._schedule_next()
            self.on_update(self._stale_payload(str(err) or "fetch failed"))
            return

        if not windows:
            self._schedule_next()
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
        self._schedule_next()
        payload = {"windows": windows, "stale": False, "at": self.last_good_at}
        if self._save_failed:
            payload["warn"] = "refreshed but could not save - run `claude`"
        self.on_update(payload)

    def _schedule_next(self) -> None:
        self._next_fetch = time.time() + self.interval

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

        # Bars hidden by the user, held by `kind` key rather than label so the
        # setting survives a display-name change (e.g. the premium model being
        # renamed). `known` remembers every bar we have seen so a hidden one can
        # still be listed in the menu and turned back on.
        self.hidden: set[str] = set(self.state.get("hidden_bars") or [])
        self.known: list[tuple[str, str]] = [
            (str(k), str(v)) for k, v in (self.state.get("known_bars") or [])
        ]
        # Submenus must stay referenced or a collected one breaks the cascade.
        self._menus: list = []
        self._tick = self._pick_tick()
        self._closing = False

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
            target.bind("<ButtonRelease-1>", self._drag_end)
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
            # Once only: Windows tucks new tray icons into the overflow, so say
            # where the window went rather than letting it seem to disappear.
            if not self.state.get("tray_hint_shown"):
                self.state["tray_hint_shown"] = True
                self.tray.notify(
                    "Claude usage widget",
                    "Still running in the tray. Click the icon to bring it back "
                    "- it may be under the ^ overflow arrow.",
                )
                self._save()
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
        self._moved = False

    def _drag_move(self, event) -> None:
        x = self._wx + (event.x_root - self._ox)
        y = self._wy + (event.y_root - self._oy)
        self.root.geometry(f"+{x}+{y}")
        self._moved = True

    def _drag_end(self, _event) -> None:
        # Save on release rather than only at quit, so the position survives the
        # process being killed rather than closed.
        if getattr(self, "_moved", False):
            self._moved = False
            self._save()

    def _new_menu(self, parent):
        return self.tk.Menu(parent, tearoff=0, bg=BG, fg=FG,
                            activebackground="#333", activeforeground=FG,
                            borderwidth=0)

    def _pick_tick(self) -> str:
        """Choose a tick glyph the menu font can actually draw."""
        try:
            from tkinter import font as tkfont
            menu_font = tkfont.nametofont("TkMenuFont")
            tick = menu_font.measure("✓")
            # A Private Use Area codepoint has no glyph in any real font. If the
            # tick measures the same, both are rendering as the missing-glyph
            # box, so fall back to something unambiguously drawable.
            if tick and tick != menu_font.measure(""):
                return "✓"
        except Exception:
            pass
        return "*"

    def _mark(self, text: str, on: bool | None = None) -> str:
        """Put toggle state in the label rather than the indicator.

        Tk's checkbutton indicator takes its colour from `selectColor`, which is
        unset by default and resolves to something invisible against a dark menu
        background. Drawing the tick ourselves is legible on every platform and
        theme. `on=None` means "not a toggle", indented to share the gutter.
        """
        return (f"{self._tick}  " if on else "    ") + text

    def _menu(self, event) -> None:
        menu = self._new_menu(self.root)
        menu.add_command(label=self._mark("Refresh now"),
                         command=self.poller.refresh_now)
        menu.add_command(
            label=self._mark("Minimize to tray" if self.tray_ok else "Collapse"),
            command=self.minimize,
        )

        bars = self._new_menu(menu)
        for key, label in self.known:
            bars.add_command(label=self._mark(label, key not in self.hidden),
                             command=lambda k=key: self._toggle_bar(k))
        if self.hidden:
            bars.add_separator()
            bars.add_command(label=self._mark("Show all"),
                             command=self._show_all_bars)
        menu.add_cascade(label=self._mark("Bars"), menu=bars,
                         state="normal" if self.known else "disabled")

        menu.add_command(label=self._mark("Always on top", self.pinned),
                         command=self.toggle_pin)
        menu.add_separator()
        menu.add_command(label=self._mark("Quit"), command=self.quit)

        # Keep the menus referenced; a collected submenu breaks the cascade.
        self._menus = [menu, bars]
        menu.tk_popup(event.x_root, event.y_root)

    def _toggle_bar(self, key: str) -> None:
        self.hidden.symmetric_difference_update({key})
        if self._last:
            self._render(self._last)
        self._save()

    def _show_all_bars(self) -> None:
        self.hidden.clear()
        if self._last:
            self._render(self._last)
        self._save()

    def toggle_pin(self) -> None:
        self.pinned = not self.pinned
        self.root.attributes("-topmost", self.pinned)
        self._save()

    def _save(self) -> None:
        # Written on every change, not just at quit, so settings survive a kill.
        try:
            position = f"+{self.root.winfo_x()}+{self.root.winfo_y()}"
        except Exception:
            position = self.state.get("geometry")
        save_state({
            "geometry": position,
            "pinned": self.pinned,
            "hidden_bars": sorted(self.hidden),
            "known_bars": [list(pair) for pair in self.known],
            "tray_hint_shown": bool(self.state.get("tray_hint_shown")),
        })

    def quit(self) -> None:
        self._closing = True
        self.poller.stop()
        self.tray.stop()
        self._save()
        self.root.destroy()

    # -- rendering --------------------------------------------------------
    def _on_update(self, payload: dict) -> None:
        # Poller runs off-thread; hop back onto the tk loop. A fetch in flight
        # when the window goes away would otherwise raise from the poller thread.
        if self._closing:
            return
        try:
            self.root.after(0, lambda: self._render(payload))
        except Exception:
            pass

    def _render(self, payload: dict) -> None:
        self._last = payload
        if "error" in payload:
            self._clear_rows()
            self.status.config(text=payload["error"], fg=CRIT)
            self.tray.set_tooltip(f"Claude usage\n{payload['error']}"[:127])
            return

        # Remember every bar the API reports, so hidden ones stay listed in the
        # menu and can be switched back on.
        seen = {k for k, _ in self.known}
        for win in payload["windows"]:
            if win["key"] not in seen:
                self.known.append((win["key"], win["label"]))
                seen.add(win["key"])

        visible = [w for w in payload["windows"] if w["key"] not in self.hidden]
        for win in visible:
            self._row(win)

        # Drop rows that are hidden, or that the API stopped reporting.
        live = {w["label"] for w in visible}
        for label in [k for k in self.rows if k not in live]:
            self.rows.pop(label)["frame"].destroy()

        # The tooltip is the compact view, so it honours the same hiding. If
        # everything is hidden it falls back to the full set rather than nothing.
        self.tray.set_tooltip(
            tooltip_text(visible or payload["windows"], bool(payload.get("stale")))
        )

        if not visible and payload["windows"]:
            self.status.config(text="all bars hidden - right-click › Bars", fg=DIM)
            return

        when = time.strftime("%H:%M", time.localtime(payload["at"]))
        if payload.get("warn"):
            self.status.config(text=payload["warn"], fg=WARN)
            return
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
                target.bind("<ButtonRelease-1>", self._drag_end)
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
WM_TRAY_BALLOON = WM_APP + 4

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
        self._show_message = 0  # set once the window class is registered
        self._ready = threading.Event()
        self._tip_lock = threading.Lock()
        self._pending_tip = tooltip
        self._pending_balloon: tuple[str, str] | None = None
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

    def notify(self, title: str, text: str) -> None:
        """Show a balloon. Windows hides new tray icons in the overflow, so
        without this a first minimize looks like the widget just vanished."""
        if not self.available or not self.hwnd:
            return
        with self._tip_lock:
            self._pending_balloon = (title[:63], text[:255])
        try:
            self._user32.PostMessageW(self.hwnd, WM_TRAY_BALLOON, 0, 0)
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
        self._user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        self._user32.RegisterWindowMessageW.restype = wintypes.UINT

        # A second launch broadcasts this to ask us to unhide. Registered names
        # resolve to the same message id in every process, so it crosses the
        # process boundary without us needing to know the other one's HWND.
        self._show_message = self._user32.RegisterWindowMessageW(SHOW_MESSAGE_NAME)

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
            if self._show_message and msg == self._show_message:
                self.on_restore()
                return 0
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
            if msg == WM_TRAY_BALLOON:
                self._apply_balloon()
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

    def _apply_balloon(self) -> None:
        ctypes = self._ctypes
        with self._tip_lock:
            pending = self._pending_balloon
            self._pending_balloon = None
        if not pending:
            return
        title, text = pending
        self._data.uFlags = 0x01 | 0x02 | 0x04 | 0x10  # ...| NIF_INFO
        self._data.szInfoTitle = title
        self._data.szInfo = text
        self._data.dwInfoFlags = 0x01  # NIIF_INFO
        self._shell32.Shell_NotifyIconW(0x01, ctypes.byref(self._data))  # NIM_MODIFY
        # Clear NIF_INFO again so later tooltip updates don't re-show the balloon.
        self._data.uFlags = 0x01 | 0x02 | 0x04
        self._data.szInfo = ""

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
# single instance
#
# Multiple copies each poll independently, multiplying the request rate against
# an endpoint that rate limits hard - so a second launch should surface the
# instance that already exists rather than add another poller.
#
# Windows uses a named mutex, and asks the running copy to show itself with a
# registered broadcast message. Elsewhere we take an flock on a lock file; there
# is no portable way to signal the other process, but on those platforms the
# widget never hides itself (no tray, so minimize only collapses), which means
# the existing window is already on screen.
# --------------------------------------------------------------------------

MUTEX_NAME = "Local\\ClaudeUsageWidget.SingleInstance"
SHOW_MESSAGE_NAME = "ClaudeUsageWidget.Show"
LOCK_PATH = Path.home() / ".claude-usage-widget.lock"
HWND_BROADCAST = 0xFFFF
ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    def __init__(self) -> None:
        self._handle = None   # Windows: mutex handle, held for process lifetime
        self._file = None     # POSIX: open lock file, ditto

    def acquire(self) -> bool:
        """True if we are the only instance. Keeps the lock for our lifetime."""
        if platform.system() == "Windows":
            return self._acquire_mutex()
        return self._acquire_flock()

    def _acquire_mutex(self) -> bool:
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [
                wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR
            ]
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
            if not handle:
                return True  # can't tell, so don't block the user
            if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
                return False
            self._handle = handle
            return True
        except Exception:
            return True

    def _acquire_flock(self) -> bool:
        try:
            import fcntl
        except ImportError:
            return True
        try:
            handle = open(LOCK_PATH, "w", encoding="utf-8")
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        except Exception:
            return True
        handle.write(str(os.getpid()))
        handle.flush()
        self._file = handle
        return True

    def signal_existing(self) -> None:
        """Best effort: ask the running instance to unhide itself."""
        if platform.system() != "Windows":
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
            user32.RegisterWindowMessageW.restype = wintypes.UINT
            user32.PostMessageW.argtypes = [
                wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
            ]
            message = user32.RegisterWindowMessageW(SHOW_MESSAGE_NAME)
            if message:
                user32.PostMessageW(HWND_BROADCAST, message, 0, 0)
        except Exception:
            pass


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------

def probe() -> int:
    credential = get_credential()
    if credential is None:
        print("No OAuth token found. Run `claude` once to sign in, or set "
              "CLAUDE_CODE_OAUTH_TOKEN.", file=sys.stderr)
        return 2
    if credential.expired():
        when = credential.expires_at
        stamp = datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M") if when else "?"
        print(f"Token expired at {stamp}. Run `claude` once to refresh it.",
              file=sys.stderr)
        return 4
    try:
        payload = fetch_usage(credential.token)
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
    global ALLOW_REFRESH
    if "--no-refresh" in sys.argv:
        ALLOW_REFRESH = False
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

    # Held for the process lifetime; releasing it early would let a second
    # instance start while this one is still running.
    guard = SingleInstance()
    if "--allow-multiple" not in sys.argv and not guard.acquire():
        guard.signal_existing()
        print("Claude usage widget is already running - asked it to show itself.")
        return 0

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
