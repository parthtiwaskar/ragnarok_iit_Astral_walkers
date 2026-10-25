@echo off
rem Ragnarisk demo launcher for this laptop. Double-click it, or run it from a terminal.
rem Starts the server on http://127.0.0.1:8765 (this machine only) and opens the dashboard.
rem Settings and keys come from .env in this folder. Close this window to stop the server.
setlocal
cd /d "%~dp0"
set "URL=http://127.0.0.1:8765/"
set "NINE_REALMS_EMBEDDER=hash"
set "PY=backend\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

rem Already running (for example from an earlier window)? Just open the dashboard.
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing '%URL%api/health' -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }"
if %errorlevel%==0 (
  echo Ragnarisk is already running. Opening %URL%
  if not defined NO_BROWSER start "" "%URL%"
  goto :eof
)

echo.
echo === Checking the AI key (one tiny request, well under a cent) ===
"%PY%" -m nine_realms.preflight
if errorlevel 1 (
  echo.
  echo Live answers for NEW questions will not work until the key above is fixed.
  echo Saved answers, evidence, the debate and the PDF report still work.
)

echo.
echo === Starting Ragnarisk on %URL% ===
echo Keep this window open during the demo. Close it to stop the server.
if not defined NO_BROWSER start "" /b powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 5; Start-Process '%URL%'"
"%PY%" -m nine_realms.server --host 127.0.0.1 --port 8765 --pack data/runemark
