@echo off
REM Stop all Offspring processes (Chloe daemon, Faith daemon, Chat server)
REM Safe to run multiple times. Cleans up lock files.

cd /d "C:\Users\wjcor\OneDrive\Desktop\Offspring"

echo Stopping all Offspring processes (Chloe, Faith, chat server, Chrome)...

REM Kill by WMIC process matching — tokens=1 because WMIC outputs PID as a single token
for /f "tokens=1" %%i in ('wmic process where "name='python.exe' and commandline like '%%agent.py%%'" get processid 2^>nul ^| findstr /r "[0-9]"') do (
    taskkill /PID %%i /T /F >nul 2>&1
)
for /f "tokens=1" %%i in ('wmic process where "name='python.exe' and commandline like '%%chloe_chat%%'" get processid 2^>nul ^| findstr /r "[0-9]"') do (
    taskkill /PID %%i /T /F >nul 2>&1
)
for /f "tokens=1" %%i in ('wmic process where "name='python.exe' and commandline like '%%chat_server%%'" get processid 2^>nul ^| findstr /r "[0-9]"') do (
    taskkill /PID %%i /T /F >nul 2>&1
)

REM Fallback: kill by window title — /T kills entire process tree (CMD + Python child)
taskkill /FI "WINDOWTITLE eq Chloe v2 Agent" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Faith Agent" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Chloe Chat Server" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Sibling Chat Server" /T /F >nul 2>&1

REM Kill by lock file PIDs as last resort
if exist "data\agent.lock" (
    set /p CHLOE_PID=<data\agent.lock
    taskkill /PID %CHLOE_PID% /T /F >nul 2>&1
)
if exist "data_faith\agent.lock" (
    set /p FAITH_PID=<data_faith\agent.lock
    taskkill /PID %FAITH_PID% /T /F >nul 2>&1
)

REM Kill the Chrome chat window (--app mode on port 8085)
for /f "tokens=1" %%i in ('wmic process where "name='chrome.exe' and commandline like '%%localhost:8085%%'" get processid 2^>nul ^| findstr /r "[0-9]"') do (
    taskkill /PID %%i /T /F >nul 2>&1
)

REM Clean up lock files
del /q "data\agent.lock" 2>nul
del /q "data_faith\agent.lock" 2>nul

echo.
echo All stopped. Fan should quiet down shortly.
