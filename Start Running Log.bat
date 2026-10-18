@echo off
rem Double-click this file to start Running Log on Windows.
rem The first time it sets itself up (a few minutes, needs the internet); after that it just starts.
setlocal
cd /d "%~dp0"

rem 1. Find Python 3.11 or newer.
set "PYTHON="
for %%C in ("py -3" "python") do (
  if not defined PYTHON (
    %%~C -c "import sys; sys.exit(not ((3, 11) <= sys.version_info < (3, 14)))" >nul 2>&1 && set "PYTHON=%%~C"
  )
)
if not defined PYTHON if not exist ".venv\Scripts\python.exe" (
  echo.
  echo Running Log needs Python, which isn't on this computer yet.
  echo A download page is opening in your browser: install Python from it ^(3.11, 3.12 or 3.13^)
  echo and tick "Add python.exe to PATH" in the installer. Then double-click "Start Running Log" again.
  start "" "https://www.python.org/downloads/"
  echo.
  pause
  exit /b 1
)

rem 2. First run: install what the app needs into its own folder.
if not exist ".venv\.installed" (
  echo.
  echo Setting up Running Log. This takes a few minutes the first time...
  if not exist ".venv\Scripts\python.exe" %PYTHON% -m venv .venv
  if errorlevel 1 goto :failed
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.lock || ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
  if errorlevel 1 goto :failed
  echo done> ".venv\.installed"
  echo Set up.
)

rem 3. Start the app and open it in the browser. Closing this window stops it.
echo.
echo Starting Running Log...
".venv\Scripts\python.exe" -m runapp serve --open
if errorlevel 1 goto :stopped
exit /b 0

:failed
echo.
echo Couldn't finish setting up. Check your internet connection, then double-click "Start Running Log" again.
pause
exit /b 1

:stopped
echo.
echo Running Log stopped with an error ^(shown above^).
pause
exit /b 1
