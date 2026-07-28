<#
.SYNOPSIS
    Installs the Claude usage widget for the current user.

.DESCRIPTION
    Copies the widget into %LOCALAPPDATA%\ClaudeUsageWidget, generates an icon,
    and creates Desktop and Start Menu shortcuts. Optionally adds a Startup
    shortcut so it launches at login.

    Entirely per-user: no administrator rights, no registry writes, nothing
    outside your profile.

.PARAMETER Startup
    Also launch the widget automatically at login, minimized to the tray.

.PARAMETER StartupVisible
    With -Startup, show the widget at login instead of starting in the tray.

.PARAMETER NoDesktop
    Skip the Desktop shortcut (Start Menu only).

.PARAMETER Launch
    Start the widget once installation finishes.

.PARAMETER Uninstall
    Remove shortcuts and installed files.

.EXAMPLE
    .\install.ps1 -Startup -Launch
#>
[CmdletBinding()]
param(
    [switch]$Startup,
    [switch]$StartupVisible,
    [switch]$NoDesktop,
    [switch]$Launch,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

$AppName   = 'Claude Usage Widget'
$InstallTo = Join-Path $env:LOCALAPPDATA 'ClaudeUsageWidget'
$ScriptName = 'claude_usage_widget.py'

$Shortcuts = @(
    (Join-Path ([Environment]::GetFolderPath('Desktop'))  "$AppName.lnk"),
    (Join-Path ([Environment]::GetFolderPath('Programs')) "$AppName.lnk"),
    (Join-Path ([Environment]::GetFolderPath('Startup'))  "$AppName.lnk")
)

function Write-Step($Message) { Write-Host "  $Message" -ForegroundColor DarkGray }
function Write-Ok($Message)   { Write-Host "  $Message" -ForegroundColor Green }

# ---------------------------------------------------------------- uninstall
if ($Uninstall) {
    Write-Host "`nRemoving $AppName" -ForegroundColor Cyan
    foreach ($link in $Shortcuts) {
        if (Test-Path $link) {
            Remove-Item $link -Force
            Write-Step "removed shortcut: $link"
        }
    }
    if (Test-Path $InstallTo) {
        Remove-Item $InstallTo -Recurse -Force
        Write-Step "removed $InstallTo"
    }
    $state = Join-Path $env:USERPROFILE '.claude-usage-widget.json'
    if (Test-Path $state) {
        Write-Step "kept saved position: $state (delete by hand if unwanted)"
    }
    Write-Ok 'Uninstalled.'
    return
}

# ------------------------------------------------------------ find python
# pythonw.exe is the console-less launcher, so the widget shows no black window.
$PythonW = $null
try {
    $PythonW = (Get-Command pythonw.exe -ErrorAction Stop).Source
} catch {
    try {
        $sibling = Join-Path (Split-Path (Get-Command python.exe -ErrorAction Stop).Source) 'pythonw.exe'
        if (Test-Path $sibling) { $PythonW = $sibling }
    } catch {
        $PythonW = $null
    }
}

if (-not $PythonW) {
    Write-Host @"

Python was not found on your PATH.

The widget needs Python 3.8 or newer, which is all it needs - no packages.
Install it from https://www.python.org/downloads/ or run:

    winget install Python.Python.3.13

Make sure "Add python.exe to PATH" is checked, then re-run this installer.
"@ -ForegroundColor Yellow
    exit 1
}

# tkinter ships with the python.org installer but can be absent in trimmed builds.
& $PythonW -c "import tkinter" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host @"

Found Python at $PythonW, but it has no tkinter module.

The widget draws its window with tkinter. Reinstall Python from
https://www.python.org/downloads/ with the "tcl/tk and IDLE" option enabled.
"@ -ForegroundColor Yellow
    exit 1
}

# --------------------------------------------------------------- install
Write-Host "`nInstalling $AppName" -ForegroundColor Cyan
Write-Step "python: $PythonW"

$source = Join-Path $PSScriptRoot $ScriptName
if (-not (Test-Path $source)) {
    Write-Host "  Cannot find $ScriptName next to this installer." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $InstallTo)) {
    New-Item -ItemType Directory -Path $InstallTo -Force | Out-Null
}
Copy-Item $source (Join-Path $InstallTo $ScriptName) -Force
Write-Step "installed to $InstallTo"

$target = Join-Path $InstallTo $ScriptName
$icon = Join-Path $InstallTo 'widget.ico'
& $PythonW $target --make-icon $icon | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Step 'icon generation failed; using default icon' }

function New-WidgetShortcut($Path, $Label, $ExtraArgs = '') {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($Path)
    $link.TargetPath = $PythonW
    $link.Arguments = ('"{0}" {1}' -f $target, $ExtraArgs).Trim()
    $link.WorkingDirectory = $InstallTo
    $link.Description = 'Claude usage limits at a glance'
    if (Test-Path $icon) { $link.IconLocation = $icon }
    $link.Save()
    Write-Step "$Label shortcut created"
}

New-WidgetShortcut $Shortcuts[1] 'Start Menu'
if (-not $NoDesktop) { New-WidgetShortcut $Shortcuts[0] 'Desktop' }

if ($Startup) {
    # Start in the tray at login unless asked otherwise, so it isn't in the way
    # every time you sign in. The Desktop/Start Menu shortcuts still open it.
    if ($StartupVisible) {
        New-WidgetShortcut $Shortcuts[2] 'Startup (visible at login)'
    } else {
        New-WidgetShortcut $Shortcuts[2] 'Startup (to tray at login)' '--minimized'
    }
} elseif (Test-Path $Shortcuts[2]) {
    Remove-Item $Shortcuts[2] -Force
    Write-Step 'startup entry removed (pass -Startup to keep it)'
}

Write-Ok "`n$AppName installed."
Write-Host @"
  Launch it from the Start Menu, or the Desktop shortcut.
  Right-click the widget for refresh / always-on-top / quit.

  Add launch-at-login later:  .\install.ps1 -Startup
  Remove everything:          .\install.ps1 -Uninstall
"@ -ForegroundColor DarkGray

if ($Launch) {
    Start-Process -FilePath $PythonW -ArgumentList "`"$target`"" -WorkingDirectory $InstallTo
    Write-Ok 'Launched.'
}
