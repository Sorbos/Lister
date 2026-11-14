@echo off
REM Double-click to start Lister: watches C:\Lister\inbox and opens the review page.
REM Close this window (or press Ctrl+C) to stop.
title Lister
if not exist "C:\Lister" mkdir "C:\Lister"
cd /d "C:\Lister"
lister watch
if errorlevel 1 pause
