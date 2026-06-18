@echo off
setlocal

cd /d "%~dp0"

where node >nul 2>nul
if errorlevel 1 (
  echo JSE needs Node.js first. Install the LTS version from https://nodejs.org/ and run this again.
  pause
  exit /b 1
)

where python >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul
  if errorlevel 1 (
    echo JSE needs Python 3 first. Install Python from https://www.python.org/downloads/windows/ and run this again.
    pause
    exit /b 1
  )
  set "PYTHON_BOOTSTRAP=py -3"
) else (
  set "PYTHON_BOOTSTRAP=python"
)

if not exist ".venv\Scripts\python.exe" (
  echo First run setup: creating Python virtual environment...
  %PYTHON_BOOTSTRAP% -m venv .venv
  if errorlevel 1 (
    echo Failed to create the Python virtual environment.
    pause
    exit /b 1
  )
)

set "PYTHON=%CD%\.venv\Scripts\python.exe"

echo First run setup: checking Python requirements...
"%PYTHON%" -m pip install --upgrade pip
if errorlevel 1 (
  echo Failed to upgrade pip.
  pause
  exit /b 1
)

"%PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Failed to install Python requirements.
  pause
  exit /b 1
)

if not exist "node_modules\.bin\vite.cmd" (
  echo First run setup: installing npm dependencies...
  call npm install
  if errorlevel 1 (
    echo Failed to install npm dependencies.
    pause
    exit /b 1
  )
)

call npm run start
set "JSE_EXIT=%ERRORLEVEL%"
endlocal & exit /b %JSE_EXIT%
