@echo off
setlocal
cd /d "%~dp0"

echo Repacking Job Application Assistant (fast path: UI build + package)...
echo.

if not exist "build\python\python.exe" (
  echo ERROR: Bundled Python runtime is missing.
  echo Run build_installer.ps1 once for a full build first.
  exit /b 1
)

set CSC_IDENTITY_AUTO_DISCOVERY=false
call npm run dist:win
if errorlevel 1 (
  echo.
  echo Repack FAILED - see output above.
  pause
  exit /b 1
)

echo.
echo Done. Distributables in the release folder:
dir /b "release\*.exe"
echo   release\win-unpacked\  (portable build)
pause
