@echo off
REM Double-click to start Lister: watches C:\Lister\inbox and opens the review page.
REM Close this window (or press Ctrl+C) to stop.
title Lister
if not exist "C:\Lister" mkdir "C:\Lister"
cd /d "C:\Lister"
where py >nul 2>nul
if %errorlevel%==0 (set "PY=py") else (set "PY=python")
%PY% -m lister watch
if errorlevel 1 pause
