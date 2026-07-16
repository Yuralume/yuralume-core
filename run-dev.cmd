@echo off
setlocal

echo Killing stale processes...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8002.*LISTENING"') do taskkill /PID %%a /F >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5174.*LISTENING"') do taskkill /PID %%a /F >nul 2>&1
timeout /t 1 /nobreak >nul

echo Starting Yuralume...
echo   Backend:  http://127.0.0.1:8002
echo   Frontend: http://127.0.0.1:5174
echo.

start "kokoro-backend" cmd /c "cd /d %~dp0 && uv run python -m uvicorn kokoro_link.api.app:create_app --factory --reload --host 127.0.0.1 --port 8002"
start "kokoro-frontend" cmd /c "cd /d %~dp0frontend && npx vite --strictPort"

echo Both servers started. Close the spawned windows to stop.
pause
