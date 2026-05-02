@echo off
setlocal enabledelayedexpansion

title MUSICSTREAM OPERATIONS

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
echo   [9] DB Stats + Sample Rows
echo   [0] Exit
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
echo [INFO] Building and starting stack...
docker-compose up -d --build
set STACK_ERR=%errorlevel%
echo.
echo --- Container status ---
docker-compose ps
echo.
if %STACK_ERR% neq 0 goto opt1_fail

echo [OK]   Stack started. Waiting 15 seconds for services...
timeout /t 15 /nobreak >nul
echo.
echo --- Health ---
curl -sf http://localhost:9079/health 2>nul && echo. || echo [WARN] Daemon not yet up - check logs with option 5
curl -sf http://localhost:32400/identity >nul 2>&1 && echo [OK] Plex up || echo [WARN] Plex not yet up
curl -sf http://localhost:9078/health >nul 2>&1 && echo [OK] Scrobbler up || echo [WARN] Scrobbler not yet up
goto opt1_done

:opt1_fail
echo [ERROR] docker-compose failed. Logs:
echo.
docker-compose logs --tail=20 postgres
docker-compose logs --tail=20 daemon

:opt1_done
echo.
pause
goto menu

:: ============================================================
:opt2
:: ============================================================
echo.
echo --- Container status ---
docker-compose ps
echo.
echo --- Daemon health ---
curl -s http://localhost:9079/health 2>nul || echo [DOWN] Daemon not responding
echo.
echo --- Last 5 runs ---
curl -s http://localhost:9079/status 2>nul
echo.
echo --- Plex ---
curl -s -o nul -w "HTTP %%{http_code}" http://localhost:32400/identity 2>nul
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
echo --- Track status counts ---
docker exec musicstream-postgres psql -U musicstream -d musicstream -c "SELECT status, COUNT(*) AS count FROM tracks GROUP BY status ORDER BY status;" 2>nul
if errorlevel 1 goto opt9_err
echo.
echo --- Download method breakdown ---
docker exec musicstream-postgres psql -U musicstream -d musicstream -c "SELECT download_method, COUNT(*) AS count FROM tracks WHERE download_method IS NOT NULL GROUP BY download_method ORDER BY count DESC;" 2>nul
echo.
echo --- Format breakdown ---
docker exec musicstream-postgres psql -U musicstream -d musicstream -c "SELECT format, COUNT(*) AS count FROM tracks WHERE format IS NOT NULL GROUP BY format ORDER BY count DESC;" 2>nul
echo.
echo --- 5 most recently downloaded ---
docker exec musicstream-postgres psql -U musicstream -d musicstream -c "SELECT id, title, artist, status, format, download_method FROM tracks WHERE status='downloaded' ORDER BY updated_at DESC LIMIT 5;" 2>nul
echo.
echo --- 5 most recently failed ---
docker exec musicstream-postgres psql -U musicstream -d musicstream -c "SELECT t.id, t.title, t.artist, da.method, LEFT(da.error,80) AS last_error FROM tracks t LEFT JOIN download_attempts da ON da.track_id=t.id AND da.id=(SELECT MAX(id) FROM download_attempts WHERE track_id=t.id) WHERE t.status IN ('failed','failed_validation') ORDER BY t.updated_at DESC LIMIT 5;" 2>nul
echo.
echo --- Sources ---
docker exec musicstream-postgres psql -U musicstream -d musicstream -c "SELECT name, source_type, track_count, last_scraped_at FROM sources ORDER BY last_scraped_at DESC LIMIT 10;" 2>nul
goto opt9_done
:opt9_err
echo [ERROR] Could not connect to postgres. Is the stack running?
:opt9_done
echo.
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
