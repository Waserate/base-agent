@echo off
title Base Agent Launcher

echo Stopping previous Base Agent processes...
taskkill /F /FI "WINDOWTITLE eq Base Agent" /T >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Base Dashboard" /T >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Base Watcher" /T >nul 2>&1
timeout /t 1 /nobreak >nul

echo Starting agent.py ...
start "Base Agent" cmd /k "cd /d %~dp0 && python agent.py"

echo Starting serve_dashboard.py ...
start "Base Dashboard" cmd /k "cd /d %~dp0 && python serve_dashboard.py"

echo Starting watcher.py (AI incident watcher) ...
start "Base Watcher" cmd /k "cd /d %~dp0 && python watcher.py"

echo Waiting for dashboard to start...
timeout /t 4 /nobreak >nul

echo Opening dashboard in browser...
start "" "http://localhost:8766/dashboard.html"

echo Done. Close this window anytime.
