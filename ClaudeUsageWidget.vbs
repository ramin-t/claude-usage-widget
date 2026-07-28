' Launches the widget with no console window. Double-click, or drop a shortcut
' to this file in shell:startup to have it run at login.
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = here
shell.Run "pythonw.exe """ & fso.BuildPath(here, "claude_usage_widget.py") & """", 0, False
