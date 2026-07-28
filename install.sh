#!/usr/bin/env bash
# Installs the Claude usage widget for the current user on macOS or Linux.
#
#   ./install.sh              # install, no autostart
#   ./install.sh --startup    # also launch at login
#   ./install.sh --uninstall  # remove everything
#
# Per-user only: nothing is written outside your home directory, no sudo.
set -euo pipefail

APP="Claude Usage Widget"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_TO="$HOME/.local/share/claude-usage-widget"
SCRIPT="$INSTALL_TO/claude_usage_widget.py"

DESKTOP_ENTRY="$HOME/.local/share/applications/claude-usage-widget.desktop"
AUTOSTART_ENTRY="$HOME/.config/autostart/claude-usage-widget.desktop"
LAUNCH_AGENT="$HOME/Library/LaunchAgents/dev.claude.usagewidget.plist"

STARTUP=0
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --startup)   STARTUP=1 ;;
    --uninstall) UNINSTALL=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [ "$UNINSTALL" -eq 1 ]; then
  echo "Removing $APP"
  if [ -f "$LAUNCH_AGENT" ]; then
    launchctl unload "$LAUNCH_AGENT" 2>/dev/null || true
    rm -f "$LAUNCH_AGENT"
  fi
  rm -f "$DESKTOP_ENTRY" "$AUTOSTART_ENTRY"
  rm -rf "$INSTALL_TO"
  echo "  done (saved position kept at ~/.claude-usage-widget.json)"
  exit 0
fi

PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  echo "Python 3 not found. Install it and re-run." >&2
  exit 1
fi
if ! "$PY" -c "import tkinter" >/dev/null 2>&1; then
  echo "Python has no tkinter module. On Debian/Ubuntu:" >&2
  echo "  sudo apt install python3-tk" >&2
  echo "On Fedora:  sudo dnf install python3-tkinter" >&2
  exit 1
fi

echo "Installing $APP"
echo "  python: $PY"
mkdir -p "$INSTALL_TO"
cp "$SRC_DIR/claude_usage_widget.py" "$SCRIPT"
echo "  installed to $INSTALL_TO"

if [ "$(uname -s)" = "Darwin" ]; then
  # macOS: a LaunchAgent is the supported way to start something at login.
  if [ "$STARTUP" -eq 1 ]; then
    mkdir -p "$(dirname "$LAUNCH_AGENT")"
    cat > "$LAUNCH_AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>dev.claude.usagewidget</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$SCRIPT</string></array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
PLIST
    launchctl unload "$LAUNCH_AGENT" 2>/dev/null || true
    launchctl load "$LAUNCH_AGENT"
    echo "  will launch at login"
  fi
  echo
  echo "Start it with:  $PY $SCRIPT"
  echo "macOS has no Desktop-shortcut concept for scripts; use that command,"
  echo "or wrap it in an Automator app if you want a dock icon."
else
  # Linux: a .desktop entry gives an app-launcher icon; copying it into
  # ~/.config/autostart is the XDG-standard way to start it at login.
  mkdir -p "$(dirname "$DESKTOP_ENTRY")"
  cat > "$DESKTOP_ENTRY" <<ENTRY
[Desktop Entry]
Type=Application
Name=$APP
Comment=Claude usage limits at a glance
Exec=$PY $SCRIPT
Terminal=false
Categories=Utility;Monitor;
ENTRY
  chmod +x "$DESKTOP_ENTRY"
  echo "  app launcher entry created"
  if [ "$STARTUP" -eq 1 ]; then
    mkdir -p "$(dirname "$AUTOSTART_ENTRY")"
    cp "$DESKTOP_ENTRY" "$AUTOSTART_ENTRY"
    echo "  will launch at login"
  else
    rm -f "$AUTOSTART_ENTRY"
  fi
  echo
  echo "Find it in your app launcher, or run:  $PY $SCRIPT"
fi

echo "Remove with:  ./install.sh --uninstall"
