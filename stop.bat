@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Python environment not found. See CUSTOMIZATION.md.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" "scripts\image-library.py" --stop --standalone
if errorlevel 1 pause
