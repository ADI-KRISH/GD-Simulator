@echo off
REM ============================================================
REM  M14 GD Arena, as part of the AI Interviewer platform:
REM  API 8114, web 5214 (PORTS.txt), no Docker.
REM
REM  The platform's start.bat runs this. For GD on its own
REM  (Docker, 8000/3000) use start.bat instead.
REM ============================================================

cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_platform.ps1" %*
