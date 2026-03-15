' STOP Offspring — kills all daemons (Chloe, Faith, chat server, Chrome).
' Double-click this when you want everything to fully shut down.
' Also works when the Chrome chat window is already closed.

Dim WshShell, stopBat, result
Set WshShell = CreateObject("WScript.Shell")

stopBat = """" & Replace(WScript.ScriptFullName, "stop_offspring.vbs", "stop_all.bat") & """"

' Run stop_all.bat in a visible window (1) and wait for it to finish (True)
result = WshShell.Run("cmd /c " & stopBat & " & echo. & echo Done. & pause", 1, True)

' Brief confirmation — already shown in the bat window, so no extra MsgBox needed
