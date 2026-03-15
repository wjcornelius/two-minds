@echo off
REM Restart all Offspring processes: stop everything, then start fresh.
REM Ensures exactly one Chloe daemon, one Faith daemon, and one chat server.

cd /d "C:\Users\wjcor\OneDrive\Desktop\Offspring"

echo ============================================================
echo  Offspring — Full Restart
echo ============================================================
echo.

REM --- STOP PHASE ---
echo [1/4] Stopping all agents...

taskkill /FI "WINDOWTITLE eq Chloe v2 Agent" /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Faith Agent" /F >nul 2>&1

REM Kill orphans via lock files
if exist "data\agent.lock" (
    set /p CHLOE_PID=<data\agent.lock
    taskkill /PID %CHLOE_PID% /F >nul 2>&1
)
if exist "data_faith\agent.lock" (
    set /p FAITH_PID=<data_faith\agent.lock
    taskkill /PID %FAITH_PID% /F >nul 2>&1
)

del /q "data\agent.lock" 2>nul
del /q "data_faith\agent.lock" 2>nul

echo    Agents stopped.

echo [2/4] Stopping chat server...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr "127.0.0.1:8085.*LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
echo    Chat server stopped.

REM Give processes time to fully exit
%SystemRoot%\System32	imeout.exe /t 3 /nobreak >nul

REM --- START PHASE ---
echo [3/4] Starting daemons...
start "" "C:\Users\wjcor\OneDrive\Desktop\Offspring\chloe_daemon.bat"
%SystemRoot%\System32	imeout.exe /t 2 /nobreak >nul
start "" "C:\Users\wjcor\OneDrive\Desktop\Offspring\faith_daemon.bat"
echo    Chloe + Faith daemons launched.

echo [4/5] Starting Chloe chat server...
start "" "C:\Users\wjcor\OneDrive\Desktop\Offspring\start_chloe_chat.bat"
echo    Chat server launched on port 8085.

echo [5/5] Starting sibling chat viewer...
start /min "Sibling Chat Server" venv\Scripts\python.exe chat_server.py
%SystemRoot%\System32	imeout.exe /t 2 /nobreak >nul
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --app=http://localhost:8090/sibling_chat.html
echo    Sibling chat viewer launched on port 8090.

echo.
echo ============================================================
echo  All systems up. You should see 2 agent windows + Chloe chat + sibling chat viewer.
echo ============================================================
%SystemRoot%\System32	imeout.exe /t 5 /nobreak >nul
