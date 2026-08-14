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
echo [%date% %time%] Docker ready. Waiting for internet... >> %LOGFILE%

:WAIT_INTERNET
ping -n 1 -w 2000 8.8.8.8 >nul 2>&1
if %errorlevel% == 0 goto INTERNET_READY
echo [%date% %time%] No internet, retrying in 10s... >> %LOGFILE%
timeout /t 10 /nobreak >nul
goto WAIT_INTERNET

:INTERNET_READY
echo [%date% %time%] Internet available. Running self-heal start sequence... >> %LOGFILE%
cd /d C:\musicstream
where pwsh >nul 2>&1
if %errorlevel% == 0 (
    pwsh -NoProfile -ExecutionPolicy Bypass -File "C:\musicstream\scripts\musicstream_self_heal.ps1" -Once >> %LOGFILE% 2>&1
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "C:\musicstream\scripts\musicstream_self_heal.ps1" -Once >> %LOGFILE% 2>&1
)
echo [%date% %time%] self-heal done (exit %errorlevel%) >> %LOGFILE%
echo [%date% %time%] Startup complete. >> %LOGFILE%
exit /b 0
