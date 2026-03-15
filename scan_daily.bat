@echo off
REM Chloe's Daily Cycle - Scan, Analyze, Experiment, Reflect, Report
REM Runs at noon via Windows Task Scheduler

cd /d "C:\Users\wjcor\OneDrive\Desktop\Offspring"
set PYTHONIOENCODING=utf-8
if not exist logs mkdir logs
echo [%date% %time%] Starting Chloe's daily cycle >> logs\daily.log
"C:\Users\wjcor\OneDrive\Desktop\Offspring\venv\Scripts\python.exe" daily.py >> logs\daily.log 2>&1
echo [%date% %time%] Daily cycle completed >> logs\daily.log
