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
echo   [8] Reset Failed Tracks back to pending
echo   [9] DB Stats + Sample Rows
echo   [0] Exit
echo.
echo ============================================================
echo.

set /p "CHOICE=  Select option [0-9]: "

if "%CHOICE%"=="1" goto :opt1
if "%CHOICE%"=="2" goto :opt2
if "%CHOICE%"=="3" goto :opt3
if "%CHOICE%"=="4" goto :opt4
if "%CHOICE%"=="5" goto :opt5
if "%CHOICE%"=="6" goto :opt6
if "%CHOICE%"=="7" goto :opt7
if "%CHOICE%"=="8" goto :opt8
if "%CHOICE%"=="9" goto :opt9
if "%CHOICE%"=="0" goto :opt0

echo.
echo [WARN] Invalid option. Please enter 0-9.
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
set STACK_ERR=%errorlevel%
echo.
echo --- Container status ---
docker-compose ps
echo.
if %STACK_ERR% neq 0 (
    echo [ERROR] docker-compose up failed. Showing logs for unhealthy containers...
    echo.
    echo --- postgres logs ^(last 20 lines^) ---
    docker-compose logs --tail=20 postgres
    echo.
    echo --- plex logs ^(last 20 lines^) ---
    docker-compose logs --tail=20 plex
    echo.
    echo --- daemon logs ^(last 20 lines^) ---
    docker-compose logs --tail=20 daemon
    echo.
    echo --- scrobbler logs ^(last 20 lines^) ---
    docker-compose logs --tail=20 scrobbler
) else (
    echo [OK]   Stack started.
    echo.
    echo [INFO] Waiting 15 seconds for services to initialise...
    timeout /t 15 /nobreak >nul
    echo.
    echo --- Health checks ---
    echo [INFO] Daemon ^(:9079^):
    curl -sf http://localhost:9079/health 2>nul
    if %errorlevel% neq 0 (
        echo [WARN] Daemon not yet responding on :9079. It may still be starting up.
        echo        Check logs with option [5] or run option [2] in a moment.
    ) else (
        echo.
    )
    echo.
    echo [INFO] Plex ^(:32400^):
    curl -sf http://localhost:32400/health >nul 2>&1
    if %errorlevel% neq 0 (
        echo [WARN] Plex not yet responding on :32400. It can take 1-2 minutes on first start.
    ) else (
        echo [OK]   Plex is up.
    )
    echo.
    echo [INFO] Scrobbler ^(:9078^):
    curl -sf http://localhost:9078/health >nul 2>&1
    if %errorlevel% neq 0 (
        echo [WARN] Scrobbler not yet responding on :9078.
    ) else (
        echo [OK]   Scrobbler is up.
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

echo --- Container status ---
docker-compose ps
echo.

echo --- Port bindings ---
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" 2>nul
echo.

echo --- Daemon ^(localhost:9079^) ---
curl -s http://localhost:9079/health 2>nul
if %errorlevel% neq 0 (
    echo [DOWN] Daemon not responding on :9079
    echo        Logs:
    docker-compose logs --tail=10 daemon 2>nul
) else (
    echo.
)
echo.

echo --- Plex ^(localhost:32400^) ---
curl -s -o nul -w "HTTP %%{http_code}" http://localhost:32400/identity 2>nul
if %errorlevel% neq 0 (
    echo [DOWN] Plex not responding on :32400
    echo        Logs:
    docker-compose logs --tail=10 plex 2>nul
) else (
    echo.
)
echo.

echo --- Scrobbler ^(localhost:9078^) ---
curl -s http://localhost:9078/health 2>nul
if %errorlevel% neq 0 (
    echo [DOWN] Scrobbler not responding on :9078
) else (
    echo.
)
echo.

echo --- Last 5 daemon runs ---
curl -s http://localhost:9079/status 2>nul
if %errorlevel% neq 0 (
    echo [INFO] Daemon not available - no run history
)
echo.
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
:: [8] Reset Failed Tracks to Pending
:: ============================================================
:opt8
echo.
echo ============================================================
echo   [8] Reset Failed Tracks to Pending
echo ============================================================
echo.
echo [INFO] Resetting all failed tracks to pending and clearing bad attempt history...
docker exec musicstream-postgres psql -U musicstream -d musicstream -c "UPDATE tracks SET status='pending' WHERE status='failed';" 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Could not connect to postgres. Is the stack running?
    goto :reset_done
)
docker exec musicstream-postgres psql -U musicstream -d musicstream -c "DELETE FROM download_attempts;" 2>nul
echo.
echo [INFO] Track counts after reset:
docker exec musicstream-postgres psql -U musicstream -d musicstream -c "SELECT status, COUNT(*) FROM tracks GROUP BY status ORDER BY status;" 2>nul
echo.
echo [OK]   Done. Run option [3] Force Full Sync to retry downloads.
:reset_done
echo.
pause
goto :menu

:: ============================================================
:: [9] DB Stats + Sample Rows
:: ============================================================
:opt9
echo.
echo ============================================================
echo   [9] DB Stats + Sample Rows
echo ============================================================
echo.

docker exec musicstream-postgres psql -U musicstream -d musicstream -c "SELECT status, COUNT(*) AS count FROM tracks GROUP BY status ORDER BY status;" 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Could not connect to postgres. Is the stack running?
    goto :stats_done
)

echo.
echo --- Download method breakdown ---
docker exec musicstream-postgres psql -U musicstream -d musicstream -c "SELECT download_method, COUNT(*) AS count FROM tracks WHERE download_method IS NOT NULL GROUP BY download_method ORDER BY count DESC;" 2>nul

echo.
echo --- Format breakdown ---
docker exec musicstream-postgres psql -U musicstream -d musicstream -c "SELECT format, COUNT(*) AS count FROM tracks WHERE format IS NOT NULL GROUP BY format ORDER BY count DESC;" 2>nul

echo.
echo --- 5 most recently downloaded tracks ---
docker exec musicstream-postgres psql -U musicstream -d musicstream -c "SELECT id, title, artist, status, format, download_method, LEFT(file_path,60) AS file_path FROM tracks WHERE status='downloaded' ORDER BY updated_at DESC LIMIT 5;" 2>nul

echo.
echo --- 5 most recently failed tracks ---
docker exec musicstream-postgres psql -U musicstream -d musicstream -c "SELECT t.id, t.title, t.artist, da.method, LEFT(da.error,80) AS last_error FROM tracks t LEFT JOIN download_attempts da ON da.track_id = t.id AND da.id = (SELECT MAX(id) FROM download_attempts WHERE track_id = t.id) WHERE t.status IN ('failed','failed_validation') ORDER BY t.updated_at DESC LIMIT 5;" 2>nul

echo.
echo --- Sources ---
docker exec musicstream-postgres psql -U musicstream -d musicstream -c "SELECT name, source_type, track_count, last_scraped_at FROM sources ORDER BY last_scraped_at DESC LIMIT 10;" 2>nul

:stats_done
echo.
pause
goto :menu

:: ============================================================
:: [0] Exit
:: ============================================================
:opt0
echo.
echo Goodbye.
echo.
endlocal
exit /b 0
