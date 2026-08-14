@echo off
setlocal enabledelayedexpansion

title MUSICSTREAM OPERATIONS
set "PSQL=docker exec musicstream-postgres psql -X -U musicstream -d musicstream -v ON_ERROR_STOP=1 -P pager=off"
set "PLEX_HOST_PORT=32401"
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
        if /I "%%a"=="PLEX_HOST_PORT" set "PLEX_HOST_PORT=%%b"
    )
)

if /I "%~1"=="status" (
    set "NONINTERACTIVE=1"
    set "FAST_STATUS=1"
    goto opt9
)
if /I "%~1"=="progress" (
    set "NONINTERACTIVE=1"
    set "FAST_STATUS=1"
    goto opt9
)
if /I "%~1"=="stats" (
    set "NONINTERACTIVE=1"
    goto opt9
)

:menu
cls
echo.
echo ============================================================
echo   MUSICSTREAM - Operations Menu
echo ============================================================
echo.
echo   [1] Start Stack
echo   [2] View Health
echo   [3] Force Full Sync
echo   [4] Force Integrity Check
echo   [5] View Daemon Logs  (live)
echo   [6] Backup Database
echo   [7] Stop Stack
echo   [8] Reset Failed Tracks
echo   [9] Download Progress + Operator Status
echo   [0] Exit
echo.
echo   Tip: run startup.bat status for read-only progress/status.
echo.
echo ============================================================
echo.

set /p "CHOICE=  Select option [0-9]: "

if "%CHOICE%"=="1" goto opt1
if "%CHOICE%"=="2" goto opt2
if "%CHOICE%"=="3" goto opt3
if "%CHOICE%"=="4" goto opt4
if "%CHOICE%"=="5" goto opt5
if "%CHOICE%"=="6" goto opt6
if "%CHOICE%"=="7" goto opt7
if "%CHOICE%"=="8" goto opt8
if "%CHOICE%"=="9" goto opt9
if "%CHOICE%"=="0" goto opt0

echo.
echo [WARN] Invalid option. Enter 0-9.
timeout /t 2 /nobreak >nul
goto menu

:: ============================================================
:opt1
:: ============================================================
echo.
echo [INFO] Running self-heal start sequence...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\musicstream_self_heal.ps1" -Once
set STACK_ERR=%errorlevel%
echo.
echo --- Container status ---
docker compose ps
echo.
if %STACK_ERR% neq 0 goto opt1_fail

echo [OK]   Stack started. Waiting 15 seconds for services...
timeout /t 15 /nobreak >nul
echo.
echo --- Health ---
curl -sf http://localhost:9079/health 2>nul && echo. || echo [WARN] Daemon not yet up - check logs with option 5
curl -sf http://localhost:%PLEX_HOST_PORT%/identity >nul 2>&1 && echo [OK] Plex up on :%PLEX_HOST_PORT% || echo [WARN] Plex not yet up on :%PLEX_HOST_PORT%
curl -sf http://localhost:9078/health >nul 2>&1 && echo [OK] Scrobbler up || echo [WARN] Scrobbler not yet up
goto opt1_done

:opt1_fail
echo [ERROR] Self-heal start failed. Logs:
echo.
docker compose logs --tail=20 postgres
docker compose logs --tail=20 daemon

:opt1_done
echo.
pause
goto menu

:: ============================================================
:opt2
:: ============================================================
echo.
echo --- Container status ---
docker compose ps
echo.
echo --- Daemon health ---
curl -s http://localhost:9079/health 2>nul || echo [DOWN] Daemon not responding
echo.
echo --- Last 5 runs ---
curl -s http://localhost:9079/status 2>nul
echo.
echo --- Plex ---
curl -s -o nul -w "HTTP %%{http_code}" http://localhost:%PLEX_HOST_PORT%/identity 2>nul
echo  on :%PLEX_HOST_PORT%
echo.
echo --- Scrobbler ---
curl -s http://localhost:9078/health 2>nul || echo [DOWN] Scrobbler not responding
echo.
pause
goto menu

:: ============================================================
:opt3
:: ============================================================
echo.
echo [INFO] Triggering full sync...
curl -sf -X POST http://localhost:9079/sync 2>nul && echo [OK] Queued || echo [ERROR] Daemon not reachable
echo.
pause
goto menu

:: ============================================================
:opt4
:: ============================================================
echo.
echo [INFO] Triggering integrity check...
curl -sf -X POST http://localhost:9079/integrity 2>nul && echo [OK] Queued || echo [ERROR] Daemon not reachable
echo.
pause
goto menu

:: ============================================================
:opt5
:: ============================================================
echo.
echo Press Ctrl+C to stop.
echo.
docker-compose logs --tail=200 --follow daemon
echo.
pause >nul
goto menu

:: ============================================================
:opt6
:: ============================================================
echo.
echo [INFO] Triggering backup...
curl -sf -X POST http://localhost:9079/backup 2>nul && echo [OK] Done || echo [ERROR] Daemon not reachable
echo.
pause
goto menu

:: ============================================================
:opt7
:: ============================================================
echo.
echo [INFO] Stopping stack...
docker-compose down
echo.
pause
goto menu

:: ============================================================
:opt8
:: ============================================================
echo.
echo [INFO] Resetting failed tracks to pending...
docker exec musicstream-postgres psql -U musicstream -d musicstream -c "UPDATE tracks SET status='pending' WHERE status='failed';" 2>nul
if errorlevel 1 goto opt8_err
docker exec musicstream-postgres psql -U musicstream -d musicstream -c "DELETE FROM download_attempts;" 2>nul
echo.
echo --- Track counts ---
docker exec musicstream-postgres psql -U musicstream -d musicstream -c "SELECT status, COUNT(*) FROM tracks GROUP BY status ORDER BY status;" 2>nul
echo.
echo [OK] Done. Use option 3 to trigger a sync.
goto opt8_done
:opt8_err
echo [ERROR] Could not connect to postgres. Is the stack running?
:opt8_done
echo.
pause
goto menu

:: ============================================================
:opt9
:: ============================================================
echo.
echo [INFO] Running read-only Postgres status queries. No rows will be changed.
echo.
echo --- DB check ---
%PSQL% -c "SELECT now() AS checked_at;" 2>nul
if errorlevel 1 goto opt9_err
echo.
echo --- Overall progress ---
%PSQL% -c "WITH counts AS (SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE status='downloaded') AS downloaded, COUNT(*) FILTER (WHERE status='pending') AS pending, COUNT(*) FILTER (WHERE status='downloading') AS downloading, COUNT(*) FILTER (WHERE status IN ('failed','failed_validation','timed_out')) AS failed_family, COUNT(*) FILTER (WHERE status NOT IN ('downloaded','pending','downloading','failed','failed_validation','timed_out')) AS other_status, COUNT(*) FILTER (WHERE status='downloaded' AND file_path IS NOT NULL) AS downloaded_with_path, COUNT(*) FILTER (WHERE status='downloaded' AND (download_method IS NULL OR download_method='')) AS downloaded_without_method FROM tracks) SELECT total, downloaded, pending, downloading, failed_family, other_status, ROUND(100.0 * downloaded / NULLIF(total,0), 2) AS pct_downloaded, downloaded_with_path, downloaded_without_method FROM counts;"
echo.
echo --- Track status counts ---
%PSQL% -c "SELECT status, COUNT(*) AS count FROM tracks GROUP BY status ORDER BY status;"
echo.
echo --- Downloading freshness ---
%PSQL% -c "SELECT COUNT(*) AS downloading_total, COUNT(*) FILTER (WHERE COALESCE(heartbeat_at, updated_at) >= now() - interval '30 minutes') AS fresh_under_30m, COUNT(*) FILTER (WHERE COALESCE(heartbeat_at, updated_at) < now() - interval '30 minutes' AND file_path IS NULL) AS stale_no_file_path, COUNT(*) FILTER (WHERE COALESCE(heartbeat_at, updated_at) < now() - interval '30 minutes' AND file_path IS NOT NULL) AS stale_has_file_path FROM tracks WHERE status='downloading';"
echo.
echo --- Downloading rows, oldest first ---
%PSQL% -c "SELECT id, LEFT(title,50) AS title, LEFT(artist,35) AS artist, claimed_at, heartbeat_at, now() - COALESCE(heartbeat_at, updated_at) AS age, claim_owner, attempt_count FROM tracks WHERE status='downloading' ORDER BY COALESCE(heartbeat_at, updated_at) ASC LIMIT 20;"
echo.
echo --- Stale downloading candidates, read-only ---
%PSQL% -c "SELECT t.id, LEFT(t.title,40) AS title, LEFT(t.artist,30) AS artist, t.claimed_at, t.heartbeat_at, now() - COALESCE(t.heartbeat_at, t.updated_at) AS age, t.claim_owner, t.attempt_count, last_da.method AS last_method, LEFT(last_da.error,90) AS last_error FROM tracks t LEFT JOIN LATERAL (SELECT method, error FROM download_attempts da WHERE da.track_id=t.id ORDER BY da.attempted_at DESC, da.id DESC LIMIT 1) last_da ON true WHERE t.status='downloading' AND COALESCE(t.heartbeat_at, t.updated_at) < now() - interval '30 minutes' AND t.file_path IS NULL ORDER BY COALESCE(t.heartbeat_at, t.updated_at) ASC LIMIT 20;"
echo.
echo --- Throughput from download attempts ---
%PSQL% -c "WITH attempts AS (SELECT COUNT(*) FILTER (WHERE success AND attempted_at >= now() - interval '1 hour') AS success_1h, COUNT(*) FILTER (WHERE NOT success AND attempted_at >= now() - interval '1 hour') AS fail_1h, COUNT(*) FILTER (WHERE success AND attempted_at >= now() - interval '24 hours') AS success_24h, COUNT(*) FILTER (WHERE NOT success AND attempted_at >= now() - interval '24 hours') AS fail_24h FROM download_attempts) SELECT success_1h, fail_1h, success_24h, fail_24h, ROUND(success_24h::numeric / 24.0, 1) AS success_per_hour_24h_avg FROM attempts;"
echo.
echo --- Pending ETA at last 24h success rate ---
%PSQL% -c "WITH pending AS (SELECT COUNT(*)::numeric AS pending FROM tracks WHERE status='pending'), rate AS (SELECT COUNT(*)::numeric / 24.0 AS per_hour FROM download_attempts WHERE success AND attempted_at >= now() - interval '24 hours') SELECT pending::bigint AS pending, ROUND(per_hour, 1) AS avg_successes_per_hour_24h, CASE WHEN per_hour > 0 THEN ROUND((pending / per_hour) / 24.0, 1) ELSE NULL END AS eta_days_at_24h_rate FROM pending, rate;"
if defined FAST_STATUS goto opt9_done
echo.
echo --- Per-tier attempt breakdown ---
%PSQL% -c "SELECT COALESCE(method,'unknown') AS method, COUNT(*) FILTER (WHERE success) AS success_total, COUNT(*) FILTER (WHERE NOT success) AS fail_total, COUNT(*) FILTER (WHERE success AND attempted_at >= now() - interval '1 hour') AS success_1h, COUNT(*) FILTER (WHERE NOT success AND attempted_at >= now() - interval '1 hour') AS fail_1h, COUNT(*) FILTER (WHERE success AND attempted_at >= now() - interval '24 hours') AS success_24h, COUNT(*) FILTER (WHERE NOT success AND attempted_at >= now() - interval '24 hours') AS fail_24h, MAX(attempted_at) AS last_attempt FROM download_attempts GROUP BY 1 ORDER BY success_24h DESC, fail_24h DESC, success_total DESC LIMIT 20;"
echo.
echo --- Final download method breakdown ---
%PSQL% -c "SELECT COALESCE(download_method,'unknown') AS download_method, COUNT(*) AS downloaded FROM tracks WHERE status='downloaded' GROUP BY 1 ORDER BY downloaded DESC LIMIT 20;"
echo.
echo --- Format breakdown ---
%PSQL% -c "SELECT COALESCE(format,'unknown') AS format, COUNT(*) AS count FROM tracks WHERE status='downloaded' GROUP BY 1 ORDER BY count DESC;"
echo.
echo --- 10 most recently downloaded ---
%PSQL% -c "SELECT id, LEFT(title,45) AS title, LEFT(artist,30) AS artist, format, download_method, updated_at FROM tracks WHERE status='downloaded' ORDER BY updated_at DESC LIMIT 10;"
echo.
echo --- 10 most recently failed ---
%PSQL% -c "SELECT t.id, LEFT(t.title,40) AS title, LEFT(t.artist,30) AS artist, t.status, t.updated_at, last_da.method AS last_method, LEFT(last_da.error,80) AS last_error FROM tracks t LEFT JOIN LATERAL (SELECT method, error FROM download_attempts da WHERE da.track_id=t.id ORDER BY da.attempted_at DESC, da.id DESC LIMIT 1) last_da ON true WHERE t.status IN ('failed','failed_validation','timed_out') ORDER BY t.updated_at DESC LIMIT 10;"
echo.
echo --- Sources ---
%PSQL% -c "SELECT name, source_type, track_count, last_scraped_at FROM sources ORDER BY last_scraped_at DESC LIMIT 10;"
goto opt9_done
:opt9_err
echo [ERROR] Could not connect to postgres. Is the stack running?
set "OPT9_EXIT=1"
:opt9_done
echo.
if defined NONINTERACTIVE (
    if defined OPT9_EXIT (
        endlocal
        exit /b 1
    )
    endlocal
    exit /b 0
)
pause
goto menu

:: ============================================================
:opt0
:: ============================================================
echo.
echo Goodbye.
echo.
endlocal
exit /b 0
