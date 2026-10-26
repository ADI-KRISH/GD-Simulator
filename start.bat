@echo off
rem Double-click to start the whole GD Platform (Docker, database, backend, frontend).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
