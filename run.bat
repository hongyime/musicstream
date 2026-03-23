@echo off
set "NON_INTERACTIVE="
if "%~1"=="--non-interactive" set "NON_INTERACTIVE=1"

echo ========================================
echo music-download-code - Interactive Menu
echo ========================================
echo.

REM Check if Python 3.11+ is available
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python 3.11+ is required but not found in PATH
    echo Please install Python 3.11+ from https://www.python.org/downloads/
    if not defined NON_INTERACTIVE pause
    exit /b 1
)

REM Check FFmpeg availability
where ffmpeg >nul 2>&1
if %errorlevel% neq 0 (
    if not exist "ffmpeg.exe" (
        echo FFmpeg not found. Download from: https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip
        echo Extract ffmpeg.exe to this directory and run again.
        if not defined NON_INTERACTIVE pause
        exit /b 1
    ) else (
        set "PATH=%CD%;%PATH%"
    )
)

REM Create virtual environment if it doesn't exist
if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo ERROR: Failed to create virtual environment
        if not defined NON_INTERACTIVE pause
        exit /b 1
    )
)

REM Activate virtual environment
call venv\Scripts\activate.bat

REM Install requirements only if marker file is missing or requirements.txt is newer
if not exist "venv\.deps_installed" (
    echo Installing requirements...
    pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo ERROR: Failed to install requirements
        pause
        exit /b 1
    )
    copy /y nul "venv\.deps_installed" >nul 2>&1
) else (
    for %%A in (requirements.txt) do set REQ_DATE=%%~tA
    for %%A in ("venv\.deps_installed") do set MARKER_DATE=%%~tA
    if "%REQ_DATE%" gtr "%MARKER_DATE%" (
        echo Requirements changed, updating...
        pip install -r requirements.txt
        if %errorlevel% neq 0 (
            echo ERROR: Failed to install requirements
            pause
            exit /b 1
        )
        copy /y nul "venv\.deps_installed" >nul 2>&1
    )
)

:menu
echo.
echo What would you like to do?
echo.
echo [1] Scrape Spotify playlists ^& Liked Songs only (discover tracks)
echo [2] Resolve tracks on YouTube Music (find matches for pending tracks)
echo [3] Download audio files (download resolved tracks)
echo [4] Show status (view progress summary)
echo [5] Retry failed downloads (try failed tracks again)
echo [6] Validate project (lint + type checks + tests)
echo [7] Run chaos robustness test
echo [8] Exit
echo.
set /p choice="Choose an option (1-8): "

if "%choice%"=="1" goto scrape
if "%choice%"=="2" goto resolve
if "%choice%"=="3" goto download
if "%choice%"=="4" goto status
if "%choice%"=="5" goto retry
if "%choice%"=="6" goto validate
if "%choice%"=="7" goto chaostest
if "%choice%"=="8" goto cleanup

echo Invalid choice. Please try again.
goto menu

:scrape
echo.
echo --- Scrape Spotify Playlists ^& Liked Songs only ---
set /p fresh="Start fresh from scratch? (y/N): "
if /i "%fresh%"=="y" (
    set args=--fresh
) else (
    set args=
)
python main.py scrape %args%
echo.
echo Press any key to return to main menu...
pause >nul
goto menu

:resolve
echo.
echo --- Resolve Tracks on YouTube Music ---
set /p fresh="Start fresh from scratch? (y/N): "
if /i "%fresh%"=="y" (
    set args=--fresh
) else (
    set args=
)
python main.py resolve %args%
echo.
echo Press any key to return to main menu...
pause >nul
goto menu

:download
echo.
echo --- Download Audio Files ---
set /p outdir="Where to save? (press Enter for 'downloads' folder): "
set /p fresh="Start fresh from scratch? (y/N): "
set args=
if not "%outdir%"=="" (
    set args=-o "%outdir%"
)
if /i "%fresh%"=="y" (
    set args=%args% --fresh
)
python main.py download %args%
echo.
echo Press any key to return to main menu...
pause >nul
goto menu

:status
echo.
echo --- Status Summary ---
python main.py status
echo.
echo Press any key to return to main menu...
pause >nul
goto menu

:retry
echo.
echo --- Retry Failed Downloads ---
set /p outdir="Output directory? (press Enter for 'downloads' folder): "
set /p fresh="Include validation failures? (y/N): "
set args=
if not "%outdir%"=="" (
    set args=-o "%outdir%"
)
if /i "%fresh%"=="y" (
    set args=%args% --fresh
)
python main.py retry %args%
echo.
echo Press any key to return to main menu...
pause >nul
goto menu

:validate
echo.
echo --- Project Validation ---
python main.py validate
echo.
echo Press any key to return to main menu...
pause >nul
goto menu

:chaostest
echo.
echo --- Chaos Robustness Test ---
set /p intensity="Chaos intensity (low/medium/high, default medium): "
set /p duration="Duration in seconds (default 20): "
set args=
if "%intensity%"=="" (
    set intensity=medium
)
if "%duration%"=="" (
    set duration=20
)
set args=--chaos-intensity %intensity% --duration-seconds %duration%
python main.py chaos-test %args%
echo.
echo Press any key to return to main menu...
pause >nul
goto menu

:cleanup
echo.
echo Cleaning up environment...
deactivate
echo Goodbye!
exit /b 0
