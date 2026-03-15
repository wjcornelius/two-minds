@echo off
REM Faith — Autonomous Agent Loop (Chloe's younger sister)
REM
REM No restart loop — agent.py handles its own crash recovery.
REM Duplicate prevention: agent.py uses atomic PID lock (O_CREAT|O_EXCL).
REM If this window closes, Faith stops. Relaunch via start_faith.vbs or this bat.

cd /d "C:\Users\wjcor\OneDrive\Desktop\Offspring"
set PYTHONIOENCODING=utf-8
title Faith Agent

"C:\Users\wjcor\OneDrive\Desktop\Offspring\venv\Scripts\python.exe" -u agent.py --entity faith %*

echo.
echo Faith daemon has exited. Close this window or press any key.
pause >nul
