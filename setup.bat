@echo off
setlocal EnableExtensions
echo ========================================
echo music-download-code - Setup ^& Installation
echo ========================================

REM Check for administrator privileges
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARN] Running without administrator privileges
    echo Some operations may fail. Consider running as administrator.
    echo.
)

REM Check Python installation
echo [INFO] Checking Python installation...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found in PATH
    echo.
    echo Please install Python 3.11+ from:
    echo https://www.python.org/downloads/
    echo.
    echo Make sure to check "Add Python to PATH" during installation
    exit /b 1
)

REM Get Python version
for /f "tokens=2" %%i in ('python --version') do set PYTHON_VERSION=%%i
echo [OK] Found Python %PYTHON_VERSION%

REM Check Python version (minimum 3.11)
python -c "import sys; exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python 3.11+ required, found %PYTHON_VERSION%
    echo Please upgrade Python from https://www.python.org/downloads/
    exit /b 1
)

REM Check pip
echo [INFO] Checking pip...
python -m pip --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] pip not found
    echo Installing pip...
    python -m ensurepip --upgrade
)

REM Upgrade pip
echo [INFO] Upgrading pip...
python -m pip install --upgrade pip >nul 2>&1

REM Check Git (optional but recommended)
echo [INFO] Checking Git installation...
where git >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] Git found
) else (
    echo [WARN] Git not found ^(optional^)
    echo Git is recommended for better yt-dlp installation
    echo Download from: https://git-scm.com/download/win
)

REM Check FFmpeg
echo [INFO] Checking FFmpeg...
where ffmpeg >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] FFmpeg found in PATH
) else (
    if exist "ffmpeg.exe" (
        echo [OK] FFmpeg found in local directory
        set "PATH=%CD%;%PATH%"
    ) else (
        echo [WARN] FFmpeg not found
        echo [INFO] Downloading FFmpeg...
        
        REM Download FFmpeg (latest build)
        echo Downloading FFmpeg Windows build...
        powershell -Command "Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile 'ffmpeg.zip'" >nul 2>&1
        
        if %errorlevel% equ 0 (
            echo [INFO] Extracting FFmpeg...
            powershell -Command "Expand-Archive -Path 'ffmpeg.zip' -DestinationPath '.' -Force" >nul 2>&1
            
            REM Find and move ffmpeg.exe
            for /d %%d in (ffmpeg-*) do (
                if exist "%%d\bin\ffmpeg.exe" (
                    copy "%%d\bin\ffmpeg.exe" "ffmpeg.exe" >nul 2>&1
                )
            )
            
            REM Cleanup
            del ffmpeg.zip >nul 2>&1
            for /d %%i in (ffmpeg-*) do rmdir /s /q "%%i" >nul 2>&1
            
            if exist "ffmpeg.exe" (
                echo [OK] FFmpeg downloaded successfully
                set "PATH=%CD%;%PATH%"
            ) else (
                echo [ERROR] Failed to download FFmpeg
                echo Please download manually from:
                echo https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip
                echo Extract ffmpeg.exe to this folder
            )
        ) else (
            echo [ERROR] Failed to download FFmpeg
            echo Please download manually from:
            echo https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip
        )
    )
)

REM Verify FFmpeg works
ffmpeg -version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] FFmpeg verification failed
    echo Please check FFmpeg installation
) else (
    echo [OK] FFmpeg working correctly
)

REM Check if virtual environment exists and is functional
if exist "venv\Scripts\activate.bat" (
    echo [INFO] Virtual environment already exists and is functional
    echo To recreate it, delete the venv folder manually and run setup again.
) else (
    echo [INFO] Creating virtual environment...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment
        exit /b 1
    )
    echo [OK] Virtual environment created
)

REM Activate virtual environment
echo [INFO] Activating virtual environment...
call venv\Scripts\activate.bat

REM Upgrade pip in virtual environment
echo [INFO] Upgrading pip in virtual environment...
python -m pip install --upgrade pip >nul 2>&1

REM Install requirements
echo [INFO] Installing Python dependencies...
if exist "requirements.txt" (
    echo Installing from requirements.txt...
    pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to install some dependencies
        echo Trying individual installations...
        
        REM Try installing core packages individually
        echo Installing core packages...
        pip install "spotipy>=2.23.0"
        pip install "ytmusicapi>=1.3.0"
        pip install "SQLAlchemy>=2.0.0"
        pip install "mutagen>=1.47.0"
        pip install "requests>=2.31.0"
        pip install "python-dateutil>=2.8.0"
        
        REM Install yt-dlp from GitHub
        echo Installing yt-dlp...
        pip install yt-dlp[default]@git+https://github.com/yt-dlp/yt-dlp.git@master
    )
) else (
    echo [ERROR] requirements.txt not found
    echo Installing core packages manually...
    pip install spotipy ytmusicapi SQLAlchemy mutagen requests python-dateutil
    pip install yt-dlp[default]@git+https://github.com/yt-dlp/yt-dlp.git@master
)

REM Write dependency marker for run.bat incremental checks
copy /y nul "venv\.deps_installed" >nul 2>&1

REM Verify installations
echo [INFO] Verifying installations...
python -c "import spotipy; print('spotipy:', spotipy.__version__)" 2>nul
python -c "import ytmusicapi; print('ytmusicapi')" 2>nul
python -c "import sqlalchemy; print('SQLAlchemy:', sqlalchemy.__version__)" 2>nul
python -c "import mutagen; print('mutagen')" 2>nul
python -c "import yt_dlp; print('yt-dlp')" 2>nul

REM Test basic functionality
echo [INFO] Testing basic functionality...
python -c "import spotipy; from spotipy.oauth2 import SpotifyOAuth; print('Spotify API import successful')" >nul 2>&1
python -c "from ytmusicapi import YTMusic; ytm = YTMusic(); print('YouTube Music API import successful')" >nul 2>&1

REM Create directories
echo [INFO] Creating directories...
if not exist "downloads" mkdir downloads
if not exist "logs" mkdir logs

REM Create sample .env file if it doesn't exist
if not exist ".env" (
    echo [INFO] Creating sample .env file...
    echo # Spotify API Client ID (get from developer.spotify.com/dashboard) > .env
    echo SPOTIFY_CLIENT_ID= >> .env
    echo. >> .env
)

REM Ensure .env ends with newline to prevent append issues
if exist ".env" (
    echo. >> .env 2>nul
)

REM Create cookies.txt placeholder
if not exist "cookies.txt" (
    echo [INFO] Creating cookies.txt placeholder...
    echo # Export your YouTube Music cookies here to avoid blocks > cookies.txt
    echo # Use a browser extension like "Get cookies.txt LOCALLY" >> cookies.txt
)

echo.
echo ========================================
echo [OK] Setup Complete!
echo ========================================
echo.
echo Next steps:
echo 1. Set up your Spotify API credentials:
echo    - Visit: https://developer.spotify.com/dashboard/applications
echo    - Create a new app
echo    - Add redirect URI: http://127.0.0.1:8888/callback
echo    - Copy Client ID to .env file (no Client Secret needed)
echo.
echo 2. (Optional) Export YouTube Music cookies:
echo    - Install "Get cookies.txt LOCALLY" browser extension
echo    - Visit music.youtube.com while logged in
echo    - Export cookies to cookies.txt file
echo.
echo 3. Run music-download-code:
echo    run.bat scrape      (discover Spotify tracks only)
echo    run.bat download    (download resolved tracks)
echo.
echo For troubleshooting, check README.md
echo.
exit /b 0
