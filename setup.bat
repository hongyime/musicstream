@echo off
setlocal enabledelayedexpansion

:: ============================================================
:: MUSICSTREAM SETUP - One-time initialisation
:: ============================================================

title MUSICSTREAM SETUP

echo.
echo ============================================================
echo   MUSICSTREAM SETUP - One-time initialisation
echo ============================================================
echo.

:: --- Elevation check -----------------------------------------
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARN] Not running as Administrator.
    echo        Firewall configuration will be skipped.
    echo        Re-run as Administrator to configure firewall rules.
    echo.
    set ADMIN=0
) else (
    set ADMIN=1
)

:: ============================================================
:: STEP 1/9 - Check prerequisites
:: ============================================================
echo [STEP 1/9] Checking prerequisites...
echo.

:: Python 3.12+
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found in PATH.
    echo         Install Python 3.12+ from https://www.python.org/downloads/
    echo         and ensure "Add Python to PATH" is checked.
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo [OK]   Python %PY_VER% found.

python -c "import sys; exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python 3.12+ required. Found %PY_VER%.
    echo         Upgrade from https://www.python.org/downloads/
    exit /b 1
)

:: Docker Desktop
docker info >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Docker Desktop is not running or not installed.
    echo         Install from https://www.docker.com/products/docker-desktop/
    echo         and start Docker Desktop before running setup.
    exit /b 1
)
echo [OK]   Docker Desktop is running.

:: Tailscale
tailscale ip -4 >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARN] Tailscale not found or not connected.
    echo        Install from https://tailscale.com/download/windows
    echo        TAILSCALE_IP will need to be set manually in .env
    set TAILSCALE_IP=127.0.0.1
) else (
    for /f %%i in ('tailscale ip -4 2^>nul') do set TAILSCALE_IP=%%i
    echo [OK]   Tailscale connected. IP: !TAILSCALE_IP!
)

:: FFmpeg
ffmpeg -version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] FFmpeg not found in PATH.
    echo         Download from https://www.gyan.dev/ffmpeg/builds/
    echo         and add the bin/ folder to your PATH.
    exit /b 1
)
echo [OK]   FFmpeg found.

:: Chromaprint (fpcalc)
fpcalc -version >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARN] fpcalc (chromaprint) not found in PATH.
    echo        AcoustID fingerprinting will be unavailable.
    echo        Download from https://acoustid.org/chromaprint
) else (
    echo [OK]   fpcalc (chromaprint) found.
)

echo.
echo [STEP 1/9] Prerequisites check complete.
echo.

:: ============================================================
:: STEP 2/9 - Configure .env
:: ============================================================
echo [STEP 2/9] Configuring .env...
echo.

if exist ".env" (
    echo [INFO] Existing .env found. Press Enter to keep current value.
    echo.
)

call :read_env SPOTIFY_CLIENT_ID
call :read_env LISTENBRAINZ_TOKEN
call :read_env LISTENBRAINZ_USERNAME
call :read_env POSTGRES_PASSWORD
call :read_env EXTERNAL_MEDIA_DRIVE
call :read_env PLEX_CLAIM_TOKEN
call :read_env PLEX_USERNAME
call :read_env PLEX_TOKEN
call :read_env PLEX_LIBRARY_SECTION_ID
call :read_env ACOUSTID_API_KEY

echo SPOTIFY_CLIENT_ID
echo   Get from: https://developer.spotify.com/dashboard
if defined SPOTIFY_CLIENT_ID echo   Current: !SPOTIFY_CLIENT_ID!
set /p "INPUT=  Enter value (Enter to keep): "
if not "!INPUT!"=="" set SPOTIFY_CLIENT_ID=!INPUT!
set INPUT=
echo.

echo LISTENBRAINZ_TOKEN
echo   Get from: https://listenbrainz.org/profile/
if defined LISTENBRAINZ_TOKEN echo   Current: !LISTENBRAINZ_TOKEN!
set /p "INPUT=  Enter value (Enter to keep): "
if not "!INPUT!"=="" set LISTENBRAINZ_TOKEN=!INPUT!
set INPUT=
echo.

echo LISTENBRAINZ_USERNAME
echo   Your ListenBrainz username
if defined LISTENBRAINZ_USERNAME echo   Current: !LISTENBRAINZ_USERNAME!
set /p "INPUT=  Enter value (Enter to keep): "
if not "!INPUT!"=="" set LISTENBRAINZ_USERNAME=!INPUT!
set INPUT=
echo.

echo POSTGRES_PASSWORD
echo   Password for the musicstream PostgreSQL user
if defined POSTGRES_PASSWORD echo   Current: [set]
set /p "INPUT=  Enter value (Enter to keep): "
if not "!INPUT!"=="" set POSTGRES_PASSWORD=!INPUT!
set INPUT=
echo.

echo EXTERNAL_MEDIA_DRIVE
echo   Path to your external HDD (e.g. E:\Music)
if defined EXTERNAL_MEDIA_DRIVE echo   Current: !EXTERNAL_MEDIA_DRIVE!
set /p "INPUT=  Enter value (Enter to keep): "
if not "!INPUT!"=="" set EXTERNAL_MEDIA_DRIVE=!INPUT!
set INPUT=
echo.

echo PLEX_CLAIM_TOKEN
echo   Get from: https://www.plex.tv/claim/ (valid for 4 minutes)
if defined PLEX_CLAIM_TOKEN echo   Current: !PLEX_CLAIM_TOKEN!
set /p "INPUT=  Enter value (Enter to keep): "
if not "!INPUT!"=="" set PLEX_CLAIM_TOKEN=!INPUT!
set INPUT=
echo.

echo PLEX_USERNAME
echo   Your Plex account username or email
if defined PLEX_USERNAME echo   Current: !PLEX_USERNAME!
set /p "INPUT=  Enter value (Enter to keep): "
if not "!INPUT!"=="" set PLEX_USERNAME=!INPUT!
set INPUT=
echo.

echo PLEX_TOKEN
echo   Get from: https://support.plex.tv/articles/204059436
if defined PLEX_TOKEN echo   Current: [set]
set /p "INPUT=  Enter value (Enter to keep): "
if not "!INPUT!"=="" set PLEX_TOKEN=!INPUT!
set INPUT=
echo.

echo PLEX_LIBRARY_SECTION_ID
echo   Numeric ID of your Plex music library section (usually 1 or 2)
if defined PLEX_LIBRARY_SECTION_ID echo   Current: !PLEX_LIBRARY_SECTION_ID!
set /p "INPUT=  Enter value (Enter to keep): "
if not "!INPUT!"=="" set PLEX_LIBRARY_SECTION_ID=!INPUT!
set INPUT=
echo.

echo ACOUSTID_API_KEY
echo   Get from: https://acoustid.org/api-key
if defined ACOUSTID_API_KEY echo   Current: !ACOUSTID_API_KEY!
set /p "INPUT=  Enter value (Enter to keep): "
if not "!INPUT!"=="" set ACOUSTID_API_KEY=!INPUT!
set INPUT=
echo.

:: Write .env
(
    echo # musicstream .env - generated by setup.bat
    echo # DO NOT COMMIT THIS FILE
    echo.
    echo # Spotify API (no client secret needed - uses PKCE)
    echo # Get from: https://developer.spotify.com/dashboard
    echo SPOTIFY_CLIENT_ID=!SPOTIFY_CLIENT_ID!
    echo.
    echo # ListenBrainz
    echo # Get token from: https://listenbrainz.org/profile/
    echo LISTENBRAINZ_TOKEN=!LISTENBRAINZ_TOKEN!
    echo LISTENBRAINZ_USERNAME=!LISTENBRAINZ_USERNAME!
    echo.
    echo # PostgreSQL
    echo POSTGRES_PASSWORD=!POSTGRES_PASSWORD!
    echo DATABASE_URL=postgresql://musicstream:!POSTGRES_PASSWORD!@localhost:5432/musicstream
    echo.
    echo # External media drive (no trailing slash)
    echo # Example: E:\Music
    echo EXTERNAL_MEDIA_DRIVE=!EXTERNAL_MEDIA_DRIVE!
    echo.
    echo # Plex Media Server
    echo # Claim token: https://www.plex.tv/claim/ (valid 4 min)
    echo PLEX_CLAIM_TOKEN=!PLEX_CLAIM_TOKEN!
    echo PLEX_USERNAME=!PLEX_USERNAME!
    echo # Token: https://support.plex.tv/articles/204059436
    echo PLEX_TOKEN=!PLEX_TOKEN!
    echo PLEX_LIBRARY_SECTION_ID=!PLEX_LIBRARY_SECTION_ID!
    echo.
    echo # Tailscale (auto-detected)
    echo TAILSCALE_IP=!TAILSCALE_IP!
    echo.
    echo # AcoustID fingerprinting
    echo # Get from: https://acoustid.org/api-key
    echo ACOUSTID_API_KEY=!ACOUSTID_API_KEY!
) > .env

echo [OK]   .env written.
echo.

:: ============================================================
:: STEP 3/9 - Create directories
:: ============================================================
echo [STEP 3/9] Creating directories...

for %%d in (backups logs "plex\config" "plex\transcode" "scrobbler\config" downloads temp) do (
    if not exist %%d (
        mkdir %%d >nul 2>&1
        echo [OK]   Created %%d
    ) else (
        echo [OK]   %%d already exists
    )
)
echo.

:: ============================================================
:: STEP 4/9 - Generate scrobbler config
:: ============================================================
echo [STEP 4/9] Generating scrobbler/config/config.yaml...

(
    echo sources:
    echo   - name: musicstream-plex
    echo     type: plex
    echo     polling:
    echo       interval: 10
    echo     data:
    echo       user: !PLEX_USERNAME!
    echo       token: !PLEX_TOKEN!
    echo.
    echo scrobbles:
    echo   - name: musicstream-lb
    echo     type: listenbrainz
    echo     data:
    echo       token: !LISTENBRAINZ_TOKEN!
) > scrobbler\config\config.yaml

echo [OK]   scrobbler/config/config.yaml generated.
echo.

:: ============================================================
:: STEP 5/9 - Configure Windows Defender Firewall
:: ============================================================
echo [STEP 5/9] Configuring Windows Defender Firewall...

if "%ADMIN%"=="0" (
    echo [SKIP] Not running as Administrator - firewall rules skipped.
    echo        Re-run setup.bat as Administrator to configure firewall.
    echo.
    goto :step6
)

for /f "tokens=*" %%i in ('powershell -NoProfile -Command "Get-NetAdapter | Where-Object {$_.InterfaceDescription -like '*Tailscale*'} | Select-Object -ExpandProperty Name" 2^>nul') do set TAILSCALE_IF=%%i

if "!TAILSCALE_IF!"=="" (
    echo [WARN] Could not detect Tailscale adapter. Allowing TCP 32400 on all interfaces.
    powershell -NoProfile -Command "New-NetFirewallRule -DisplayName 'Plex TCP 32400' -Direction Inbound -Protocol TCP -LocalPort 32400 -Action Allow -Profile Any -ErrorAction SilentlyContinue" >nul 2>&1
) else (
    echo [INFO] Tailscale adapter: !TAILSCALE_IF!
    powershell -NoProfile -Command "Remove-NetFirewallRule -DisplayName 'Plex TCP 32400*' -ErrorAction SilentlyContinue" >nul 2>&1
    powershell -NoProfile -Command "New-NetFirewallRule -DisplayName 'Plex TCP 32400 Tailscale Allow' -Direction Inbound -Protocol TCP -LocalPort 32400 -Action Allow -InterfaceAlias '!TAILSCALE_IF!' -Profile Any" >nul 2>&1
    powershell -NoProfile -Command "New-NetFirewallRule -DisplayName 'Plex TCP 32400 Block Others' -Direction Inbound -Protocol TCP -LocalPort 32400 -Action Block -Profile Any" >nul 2>&1
    echo [OK]   Firewall: TCP 32400 allowed on Tailscale, blocked elsewhere.
)
echo.

:step6
:: ============================================================
:: STEP 6/9 - docker-compose pull
:: ============================================================
echo [STEP 6/9] Pulling Docker images...
docker-compose pull
if %errorlevel% neq 0 (
    echo [WARN] docker-compose pull reported errors. Check your internet connection.
) else (
    echo [OK]   Docker images pulled.
)
echo.

:: ============================================================
:: STEP 7/9 - Start postgres and run migrations
:: ============================================================
echo [STEP 7/9] Starting PostgreSQL and running Alembic migrations...

docker-compose up -d postgres
if %errorlevel% neq 0 (
    echo [ERROR] Failed to start postgres container.
    exit /b 1
)

echo [INFO] Waiting for PostgreSQL to be ready...
set /a WAIT_COUNT=0
:wait_loop
timeout /t 3 /nobreak >nul
docker-compose exec -T postgres pg_isready -U musicstream >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK]   PostgreSQL is ready.
    goto :run_migrations
)
set /a WAIT_COUNT+=1
if !WAIT_COUNT! geq 20 (
    echo [ERROR] PostgreSQL did not become ready after 60 seconds.
    echo         Check: docker-compose logs postgres
    exit /b 1
)
echo [INFO] Still waiting... (!WAIT_COUNT!/20)
goto :wait_loop

:run_migrations
echo [INFO] Running Alembic migrations...
python -m alembic upgrade head
if %errorlevel% neq 0 (
    echo [ERROR] Alembic migrations failed.
    echo         Check your DATABASE_URL in .env and try again.
    exit /b 1
)
echo [OK]   Alembic migrations complete.
echo.

:: ============================================================
:: STEP 8/9 - Validate .gitignore
:: ============================================================
echo [STEP 8/9] Validating .gitignore...

set GITIGNORE_OK=1
for %%e in (.env backups/ logs/ downloads/ temp/ *.sql cookies.txt) do (
    findstr /i /c:"%%e" .gitignore >nul 2>&1
    if !errorlevel! neq 0 (
        echo [WARN] .gitignore may be missing entry: %%e
        set GITIGNORE_OK=0
    )
)

if "%GITIGNORE_OK%"=="1" (
    echo [OK]   .gitignore looks complete.
) else (
    echo [WARN] Some .gitignore entries may be missing. Review before committing.
)
echo.

:: ============================================================
:: STEP 9/9 - Done
:: ============================================================
echo [STEP 9/9] Setup complete!
echo.
echo ============================================================
echo   MUSICSTREAM SETUP - Complete
echo ============================================================
echo.
echo   Tailscale IP : !TAILSCALE_IP!
echo   Plex URL     : http://!TAILSCALE_IP!:32400/web
echo   Daemon API   : http://localhost:9079/health
echo.
echo   Next steps:
echo     1. Run startup.bat to start the full stack
echo     2. Open Plex at http://!TAILSCALE_IP!:32400/web to finish setup
echo     3. Daemon will sync Spotify automatically every 15 minutes
echo.
echo   Useful commands:
echo     startup.bat              - Day-to-day operations menu
echo     python main.py scrape    - Manual Spotify scrape
echo     python main.py download  - Manual download run
echo     python main.py status    - Show DB status
echo.
echo ============================================================
echo.

endlocal
exit /b 0

:: ============================================================
:: Subroutine: read a value from .env into a variable
:: Usage: call :read_env VAR_NAME
:: ============================================================
:read_env
set "_VAR=%~1"
set "%_VAR%="
if not exist ".env" exit /b 0
for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
    if "%%a"=="%_VAR%" set "%_VAR%=%%b"
)
exit /b 0
