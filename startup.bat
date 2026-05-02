@echo off
setlocal enabledelayedexpansion

:: ============================================================
:: MUSICSTREAM STARTUP - Day-to-day operations menu
:: ============================================================

title MUSICSTREAM OPERATIONS

:menu
cls
echo.
echo ============================================================
echo   MUSICSTREAM - Operations Menu
echo ============================================================
echo.
echo   [1] Start Stack         docker-compose up -d --build
echo   [2] View Health         docker-compose ps + GET :9079/health
echo   [3] Force Full Sync     POST http://localhost:9079/sync
echo   [4] Force Integrity     POST http://localhost:9079/integrity
echo   [5] View Daemon Logs    docker-compose logs --tail=200 --follow daemon
echo   [6] Backup Database     POST http://localhost:9079/backup
echo   [7] Stop Stack          docker-compose down
echo   [8] Exit
echo.
echo ============================================================
echo.

set /p "CHOICE=  Select option [1-8]: "

if "%CHOICE%"=="1" goto :opt1
if "%CHOICE%"=="2" goto :opt2
if "%CHOICE%"=="3" goto :opt3
if "%CHOICE%"=="4" goto :opt4
if "%CHOICE%"=="5" goto :opt5
if "%CHOICE%"=="6" goto :opt6
if "%CHOICE%"=="7" goto :opt7
if "%CHOICE%"=="8" goto :opt8

echo.
echo [WARN] Invalid option. Please enter a number between 1 and 8.
timeout /t 2 /nobreak >nul
goto :menu

:: ============================================================
:: [1] Start Stack
:: ============================================================
:opt1
echo.
echo ============================================================
echo   [1] Starting Stack...
echo ============================================================
echo.
docker-compose up -d --build
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] docker-compose up failed. Check the output above.
) else (
    echo.
    echo [OK]   Stack started. Services:
    docker-compose ps
    echo.
    echo [INFO] Waiting 10 seconds for services to initialise...
    timeout /t 10 /nobreak >nul
    echo.
    echo [INFO] Daemon health check:
    curl -sf http://localhost:9079/health 2>nul
    if %errorlevel% neq 0 (
        echo [WARN] Daemon not yet responding on :9079. It may still be starting up.
        echo        Run option [2] in a moment to check health.
    )
)
echo.
pause
goto :menu

:: ============================================================
:: [2] View Health
:: ============================================================
:opt2
echo.
echo ============================================================
echo   [2] View Health
echo ============================================================
echo.
echo --- docker-compose ps ---
docker-compose ps
echo.
echo --- GET http://localhost:9079/health ---
curl -sf http://localhost:9079/health 2>nul
if %errorlevel% neq 0 (
    echo [WARN] Daemon not responding on :9079.
    echo        Is the stack running? Use option [1] to start it.
)
echo.
echo --- GET http://localhost:9079/status (last 5 runs) ---
curl -sf http://localhost:9079/status 2>nul
if %errorlevel% neq 0 (
    echo [WARN] Could not fetch /status from daemon.
)
echo.
pause
goto :menu

:: ============================================================
:: [3] Force Full Sync
:: ============================================================
:opt3
echo.
echo ============================================================
echo   [3] Force Full Sync Now
echo ============================================================
echo.
echo [INFO] Triggering full pipeline (Spotify sync + download)...
curl -sf -X POST http://localhost:9079/sync 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Could not reach daemon on :9079.
    echo         Is the stack running? Use option [1] to start it.
) else (
    echo.
    echo [OK]   Full sync queued. Monitor progress with option [5].
)
echo.
pause
goto :menu

:: ============================================================
:: [4] Force Integrity Check
:: ============================================================
:opt4
echo.
echo ============================================================
echo   [4] Force Integrity Check
echo ============================================================
echo.
echo [INFO] Triggering integrity check...
curl -sf -X POST http://localhost:9079/integrity 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Could not reach daemon on :9079.
    echo         Is the stack running? Use option [1] to start it.
) else (
    echo.
    echo [OK]   Integrity check queued. Monitor progress with option [5].
)
echo.
pause
goto :menu

:: ============================================================
:: [5] View Daemon Logs (live)
:: ============================================================
:opt5
echo.
echo ============================================================
echo   [5] View Daemon Logs (live)
echo   Press Ctrl+C to stop following logs and return to menu.
echo ============================================================
echo.
docker-compose logs --tail=200 --follow daemon
echo.
echo [INFO] Log stream ended. Press any key to return to menu.
pause >nul
goto :menu

:: ============================================================
:: [6] Backup Database
:: ============================================================
:opt6
echo.
echo ============================================================
echo   [6] Backup Database Now
echo ============================================================
echo.
echo [INFO] Triggering pg_dump backup...
curl -sf -X POST http://localhost:9079/backup 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Could not reach daemon on :9079.
    echo         Is the stack running? Use option [1] to start it.
) else (
    echo.
    echo [OK]   Backup triggered. Check the backups/ directory for the .sql file.
)
echo.
pause
goto :menu

:: ============================================================
:: [7] Stop Stack
:: ============================================================
:opt7
echo.
echo ============================================================
echo   [7] Stop Stack
echo ============================================================
echo.
echo [INFO] Stopping all musicstream services...
docker-compose down
if %errorlevel% neq 0 (
    echo [WARN] docker-compose down reported errors. Check the output above.
) else (
    echo [OK]   Stack stopped.
)
echo.
pause
goto :menu

:: ============================================================
:: [8] Exit
:: ============================================================
:opt8
echo.
echo Goodbye.
echo.
endlocal
exit /b 0
