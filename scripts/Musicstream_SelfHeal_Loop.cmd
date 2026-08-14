@echo off
setlocal

set "LOGFILE=C:\musicstream\logs\self_heal_launcher.log"
if not exist "C:\musicstream\logs" mkdir "C:\musicstream\logs" >nul 2>&1
echo [%date% %time%] Launching Musicstream self-heal loop >> "%LOGFILE%"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$pwsh=(Get-Command pwsh -ErrorAction SilentlyContinue).Source; if(-not $pwsh){$pwsh=(Get-Command powershell -ErrorAction Stop).Source}; Start-Process -FilePath $pwsh -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File','C:\musicstream\scripts\musicstream_self_heal.ps1','-Loop','-DockerWaitSeconds','60','-HealthWaitSeconds','90') -WindowStyle Hidden" >> "%LOGFILE%" 2>&1
echo [%date% %time%] Launcher exit %errorlevel% >> "%LOGFILE%"

endlocal
exit /b %errorlevel%
