# register-boot-task.ps1
# Run ONCE in an ELEVATED (Administrator) PowerShell to register the
# musicstream boot autostart Scheduled Task.
#
#   Right-click PowerShell -> Run as administrator, then:
#   C:\musicstream\scripts\register-boot-task.ps1
#
# Idempotent: unregisters any existing task of the same name first.

$ErrorActionPreference = 'Stop'

$TaskName   = 'MusicstreamBootAutostart'
$ScriptPath = 'C:\musicstream\scripts\musicstream-boot.ps1'

# Remove existing task if present
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Removing existing task '$TaskName'..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# Action: run the boot script hidden, no profile
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptPath`""

# Trigger: at logon of the current user, with a 60s delay to let Docker
# Desktop's WSL2 backend start coming up first.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = 'PT60S'

# Principal: run as current user, highest privileges, only when logged on
# (interactive token is needed so Docker Desktop's per-user engine is reachable).
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest

# Settings: allow long start, don't kill on idle, retry on failure.
# Built via splatting to avoid backtick line-continuations.
$settingsArgs = @{
    AllowStartIfOnBatteries    = $true
    DontStopIfGoingOnBatteries = $true
    StartWhenAvailable         = $true
    ExecutionTimeLimit         = (New-TimeSpan -Minutes 15)
    RestartCount               = 3
    RestartInterval            = (New-TimeSpan -Minutes 1)
}
$settings = New-ScheduledTaskSettingsSet @settingsArgs

$registerArgs = @{
    TaskName    = $TaskName
    Action      = $action
    Trigger     = $trigger
    Principal   = $principal
    Settings    = $settings
    Description = 'Brings up the musicstream docker-compose stack at logon after waiting for the Docker engine.'
}
Register-ScheduledTask @registerArgs

Write-Host ""
Write-Host "Registered '$TaskName'. Verifying..."
Get-ScheduledTask -TaskName $TaskName | Format-List TaskName, State

Write-Host ""
Write-Host "Test it now without rebooting:"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Get-Content C:\musicstream\logs\boot-autostart.log -Tail 20"
