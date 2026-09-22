@echo off
rem Build TimelinerX for Windows. All arguments are passed to build_windows.ps1, e.g.
rem   build_windows.bat -SkipTests -OneDir
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_windows.ps1" %*
exit /b %ERRORLEVEL%
