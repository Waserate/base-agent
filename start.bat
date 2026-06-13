@echo off
title Base Agent Launcher

echo Stopping previous Base Agent processes...
taskkill /F /FI "WINDOWTITLE eq Base Agent" /T >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Base Watcher" /T >nul 2>&1
wmic process where "commandline like '%%serve_dashboard%%'" delete >nul 2>&1
timeout /t 1 /nobreak >nul

echo Starting agent.py ...
start "Base Agent" cmd /k "cd /d %~dp0 && python agent.py"

echo Starting serve_dashboard.py (hidden) ...
wscript.exe /nologo "%~dp0launch_dashboard.vbs"

echo Starting watcher.py (AI incident watcher) ...
start "Base Watcher" cmd /k "cd /d %~dp0 && set REMEDIATION_MODE=live && python watcher.py"

echo Waiting for dashboard to start...
timeout /t 4 /nobreak >nul

echo Opening dashboard in browser...
start "" "http://localhost:8766/dashboard.html"

echo Done. Close this window anytime.
