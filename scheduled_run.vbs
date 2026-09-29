' Runs scheduled_run.bat with no visible window (used by Windows Task Scheduler).
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
CreateObject("WScript.Shell").Run """" & here & "\scheduled_run.bat""", 0, True
