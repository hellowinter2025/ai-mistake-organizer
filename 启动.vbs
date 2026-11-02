' Launch the app without showing a console window.
' MM_HEADLESS makes start.py write its output to logs\launcher.log.
Option Explicit

Dim fso, sh, base, exe
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

base = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = base

exe = base & "\.venv\Scripts\python.exe"
If Not fso.FileExists(exe) Then
  MsgBox "Python venv not found:" & vbCrLf & vbCrLf & exe & vbCrLf & vbCrLf & _
         "Run these in the project folder first:" & vbCrLf & _
         "  python -m venv .venv" & vbCrLf & _
         "  .venv\Scripts\python.exe -m pip install -r requirements.txt", _
         vbCritical, "Cannot start"
  WScript.Quit 1
End If

sh.Environment("PROCESS")("MM_HEADLESS") = "1"
' Window style 0 = hidden: no console window flashes or stays open.
sh.Run Chr(34) & exe & Chr(34) & " start.py", 0, False
