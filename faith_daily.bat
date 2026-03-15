@echo off
REM Faith's Daily Cycle - Scan, Analyze, Experiment, Reflect, Report
REM Runs at 12:30 PM via Windows Task Scheduler (offset from Chloe's noon)

cd /d "C:\Users\wjcor\OneDrive\Desktop\Offspring"
set PYTHONIOENCODING=utf-8
if not exist logs mkdir logs
echo [%date% %time%] Starting Faith's daily cycle >> logs\faith_daily.log
"C:\Users\wjcor\OneDrive\Desktop\Offspring\venv\Scripts\python.exe" daily.py --entity faith >> logs\faith_daily.log 2>&1
echo [%date% %time%] Faith daily cycle completed >> logs\faith_daily.log
