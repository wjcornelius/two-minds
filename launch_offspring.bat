@echo off
cd /d "C:\Users\wjcor\OneDrive\Desktop\Offspring"

REM ── Kill ALL existing Offspring processes ──
REM Use WMIC to find and kill any python.exe running agent.py, chat_server, or chainlit
for /f "tokens=2" %%i in ('wmic process where "name='python.exe' and commandline like '%%agent.py%%'" get processid 2^>nul ^| findstr /r "[0-9]"') do (
    taskkill /PID %%i /F >nul 2>&1
)
for /f "tokens=2" %%i in ('wmic process where "name='python.exe' and commandline like '%%chloe_chat%%'" get processid 2^>nul ^| findstr /r "[0-9]"') do (
    taskkill /PID %%i /F >nul 2>&1
)
for /f "tokens=2" %%i in ('wmic process where "name='python.exe' and commandline like '%%chat_server%%'" get processid 2^>nul ^| findstr /r "[0-9]"') do (
    taskkill /PID %%i /F >nul 2>&1
)
REM Also kill by window title as fallback
taskkill /FI "WINDOWTITLE eq Chloe v2 Agent" /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Faith Agent" /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Chloe Chat Server" /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Sibling Chat Server" /F >nul 2>&1

REM ── Clean stale locks ──
del /q "data\agent.lock" 2>nul
del /q "data_faith\agent.lock" 2>nul

REM Wait for processes to fully exit
timeout /t 2 /nobreak >nul

REM ── Start Chloe ──
start /min "Chloe v2 Agent" cmd /c "cd /d C:\Users\wjcor\OneDrive\Desktop\Offspring && set PYTHONIOENCODING=utf-8 && venv\Scripts\python.exe -u agent.py"

timeout /t 5 /nobreak >nul

REM ── Start Faith ──
start /min "Faith Agent" cmd /c "cd /d C:\Users\wjcor\OneDrive\Desktop\Offspring && set PYTHONIOENCODING=utf-8 && venv\Scripts\python.exe -u agent.py --entity faith"

timeout /t 3 /nobreak >nul

REM ── Start Chloe Chat (--headless = no auto browser tab) ──
start /min "Chloe Chat Server" cmd /c "cd /d C:\Users\wjcor\OneDrive\Desktop\Offspring && set PYTHONIOENCODING=utf-8 && venv\Scripts\chainlit.exe run chloe_chat.py --port 8085 --headless"

REM Poll for readiness instead of blind wait
:wait_ready
netstat -ano | findstr "127.0.0.1:8085.*LISTENING" >nul 2>&1
if %errorlevel% neq 0 (
    timeout /t 1 /nobreak >nul
    goto wait_ready
)

REM ── Open chat in Chrome (kill any old window first) ──
for /f "tokens=2" %%i in ('wmic process where "name='chrome.exe' and commandline like '%%localhost:8085%%'" get processid 2^>nul ^| findstr /r "[0-9]"') do taskkill /PID %%i /F >nul 2>&1
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --app=http://localhost:8085

echo All launched. Safe to close this window.
