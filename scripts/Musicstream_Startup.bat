@echo off
REM Musicstream startup script
REM Auto-placed in Windows Startup folder — runs on every login
REM Waits for Docker Desktop to be ready, then starts the compose stack.
REM Mirrors C:\telegramhunter\scripts\TelegramHunter_Startup.bat pattern.

set LOGFILE=C:\musicstream\logs\startup.log
echo [%date% %time%] Musicstream startup triggered >> %LOGFILE%

REM Wait for Docker Desktop engine (up to 300s, check every 5s)
set RETRIES=60
:WAIT_LOOP
docker info >nul 2>&1
if %errorlevel% == 0 goto DOCKER_READY
set /a RETRIES=%RETRIES%-1
if %RETRIES% == 0 goto DOCKER_TIMEOUT
echo [%date% %time%] Waiting for Docker... (%RETRIES% retries left) >> %LOGFILE%
timeout /t 5 /nobreak >nul
goto WAIT_LOOP

:DOCKER_TIMEOUT
echo [%date% %time%] ERROR: Docker not ready after 300s >> %LOGFILE%
exit /b 1

:DOCKER_READY
echo [%date% %time%] Docker ready. Starting containers... >> %LOGFILE%
cd /d C:\musicstream
docker compose up -d >> %LOGFILE% 2>&1
echo [%date% %time%] docker compose up -d done (exit %errorlevel%) >> %LOGFILE%
echo [%date% %time%] Startup complete. >> %LOGFILE%
exit /b 0
