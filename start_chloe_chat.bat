@echo off
REM Launch Chloe Chat — Chainlit server + Chrome app window
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

REM Check if server is already running on port 8085
netstat -ano | findstr "127.0.0.1:8085.*LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo Chloe Chat already running. Focusing existing window...
    goto openbrowser
)

REM Start Chainlit server minimized
REM --headless: don't auto-open a browser tab (we open Chrome ourselves below)
start /min "Chloe Chat Server" venv\Scripts\chainlit.exe run chloe_chat.py --port 8085 --headless

REM Wait for server to be ready (poll instead of blind wait)
echo Waiting for chat server...
:wait
netstat -ano | findstr "127.0.0.1:8085.*LISTENING" >nul 2>&1
if %errorlevel% neq 0 (
    timeout /t 1 /nobreak >nul
    goto wait
)

:openbrowser
REM Kill any existing Chrome --app window on this port first (prevents duplicates)
for /f "tokens=2" %%i in ('wmic process where "name='chrome.exe' and commandline like '%%localhost:8085%%'" get processid 2^>nul ^| findstr /r "[0-9]"') do taskkill /PID %%i /F >nul 2>&1

REM Open Chrome in app mode (standalone window, no address bar)
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --app=http://localhost:8085 --start-maximized
