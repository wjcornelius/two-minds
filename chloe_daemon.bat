@echo off
REM Chloe v2 — Autonomous Agent Loop
REM
REM No restart loop — agent.py handles its own crash recovery.
REM Duplicate prevention: agent.py uses atomic PID lock (O_CREAT|O_EXCL).
REM If this window closes, Chloe stops. Relaunch via start_chloe.vbs or this bat.

cd /d "C:\Users\wjcor\OneDrive\Desktop\Offspring"
set PYTHONIOENCODING=utf-8
title Chloe v2 Agent

"C:\Users\wjcor\OneDrive\Desktop\Offspring\venv\Scripts\python.exe" -u agent.py %*

echo.
echo Chloe daemon has exited. Close this window or press any key.
pause >nul
