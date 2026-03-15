' Start Chloe Agent in a visible console window (shows on taskbar)
' Placed in Startup folder as a shortcut
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """C:\Users\wjcor\OneDrive\Desktop\Offspring\chloe_daemon.bat""", 1, False
' 1 = normal visible window, False = don't wait
