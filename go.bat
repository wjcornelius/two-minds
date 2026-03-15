@echo off
cd /d "C:\Users\wjcor\OneDrive\Desktop\Offspring"
title Offspring Launcher

REM ── 1. Kill existing processes ─────────────────────────────────────────────
taskkill /FI "WINDOWTITLE eq Chloe v2 Agent" /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Faith Agent" /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Chloe Chat Server" /F >nul 2>&1
if exist "data\agent.lock" (
    set /p CHLOE_PID=<data\agent.lock
    taskkill /PID %CHLOE_PID% /F >nul 2>&1
)
if exist "data_faith\agent.lock" (
    set /p FAITH_PID=<data_faith\agent.lock
    taskkill /PID %FAITH_PID% /F >nul 2>&1
)
for /f "tokens=2" %%i in ('wmic process where "name='python.exe' and commandline like '%%agent.py%%'" get processid 2^>nul ^| findstr /r "[0-9]"') do taskkill /PID %%i /F >nul 2>&1
for /f "tokens=2" %%i in ('wmic process where "name='python.exe' and commandline like '%%chloe_chat%%'" get processid 2^>nul ^| findstr /r "[0-9]"') do taskkill /PID %%i /F >nul 2>&1
for /f "tokens=2" %%i in ('wmic process where "name='chainlit.exe' and commandline like '%%8085%%'" get processid 2^>nul ^| findstr /r "[0-9]"') do taskkill /PID %%i /F >nul 2>&1
for /f "tokens=2" %%i in ('wmic process where "name='chrome.exe' and commandline like '%%localhost:8085%%'" get processid 2^>nul ^| findstr /r "[0-9]"') do taskkill /PID %%i /F >nul 2>&1
del /q "data\agent.lock" 2>nul
del /q "data_faith\agent.lock" 2>nul
timeout /t 2 /nobreak >nul 2>&1

REM ── 2. Start Chloe daemon ──────────────────────────────────────────────────
start /min "Chloe v2 Agent" cmd /c "cd /d C:\Users\wjcor\OneDrive\Desktop\Offspring && set PYTHONIOENCODING=utf-8 && venv\Scripts\python.exe -u agent.py"
timeout /t 4 /nobreak >nul 2>&1

REM ── 3. Start Faith daemon ──────────────────────────────────────────────────
start /min "Faith Agent" cmd /c "cd /d C:\Users\wjcor\OneDrive\Desktop\Offspring && set PYTHONIOENCODING=utf-8 && venv\Scripts\python.exe -u agent.py --entity faith"
timeout /t 2 /nobreak >nul 2>&1

REM ── 4. Start chat server ───────────────────────────────────────────────────
start /min "Chloe Chat Server" cmd /c "cd /d C:\Users\wjcor\OneDrive\Desktop\Offspring && set PYTHONIOENCODING=utf-8 && venv\Scripts\chainlit.exe run chloe_chat.py --port 8085 --headless"

REM Poll port 8085 until listening (max 30s)
set TRIES=0
:wait_chat
netstat -ano | findstr "127.0.0.1:8085.*LISTENING" >nul 2>&1
if %errorlevel%==0 goto chat_ready
set /a TRIES+=1
if %TRIES% GEQ 30 goto open_chat
timeout /t 1 /nobreak >nul 2>&1
goto wait_chat

:chat_ready
REM Extra 2s for Chainlit to finish route setup before Chrome hits it
timeout /t 2 /nobreak >nul 2>&1

REM ── 5. Open chat in Chrome, then maximize via PowerShell ───────────────────
:open_chat
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --app=http://localhost:8085

REM Wait for Chrome window to appear, then maximize it (--start-maximized unreliable in --app mode)
timeout /t 3 /nobreak >nul 2>&1
powershell -WindowStyle Hidden -Command ^
  "Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;public class W{[DllImport(\"user32.dll\")]public static extern bool ShowWindow(IntPtr h,int n);}'; ^
   Get-Process chrome -ErrorAction SilentlyContinue ^
   | Where-Object {$_.MainWindowHandle -ne 0} ^
   | ForEach-Object { [W]::ShowWindow($_.MainWindowHandle, 3) }"

exit
