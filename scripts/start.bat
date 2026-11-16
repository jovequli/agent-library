@echo off
setlocal
rem Start this checkout only; do not change another app's library or launcher.
for %%I in ("%~dp0..") do set "PROJECT_ROOT=%%~fI"
set "PYTHONPATH=%PROJECT_ROOT%\src;%PYTHONPATH%"
if exist "%PROJECT_ROOT%\.venv\Scripts\python.exe" (
  "%PROJECT_ROOT%\.venv\Scripts\python.exe" -m agent_library serve --open %*
) else (
  py -3 -m agent_library serve --open %*
)
if errorlevel 1 (
  echo Agent Library needs Python 3.11 or newer. See README for setup instructions.
  pause
)
endlocal
