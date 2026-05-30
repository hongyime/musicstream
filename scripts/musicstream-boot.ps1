# musicstream-boot.ps1
# Boot/logon auto-start for the musicstream docker-compose stack.
#
# Why this exists (belt-and-suspenders over restart:unless-stopped):
#   - restart:unless-stopped only resurrects containers if the Docker engine
#     itself comes back. On Windows after a reboot, Docker Desktop's WSL2
#     backend can take 30-120s to be ready, and containers that exited 137
#     during shutdown sometimes do NOT auto-restart cleanly.
#   - This script waits for `docker info` to succeed, then explicitly runs
#     `docker compose up -d`, which is idempotent (no-op if already running).
#
# Registered as a Scheduled Task at logon with a 60s delay. See
# scripts/register-boot-task.ps1.

$ErrorActionPreference = 'Stop'
$ProjectDir = 'C:\musicstream'
$LogFile    = Join-Path $ProjectDir 'logs\boot-autostart.log'
$DockerExe  = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'

function Write-Log {
    param([string]$Message)
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $line = "$ts  $Message"
    Add-Content -Path $LogFile -Value $line
    Write-Output $line
}

# Ensure log dir exists
$logDir = Split-Path $LogFile -Parent
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}

Write-Log '=== musicstream boot autostart starting ==='

# 1. Make sure Docker Desktop is launched (AutoStart should do this, but be sure)
$dockerProc = Get-Process 'Docker Desktop' -ErrorAction SilentlyContinue
if (-not $dockerProc) {
    Write-Log 'Docker Desktop not running; launching it.'
    Start-Process -FilePath $DockerExe
} else {
    Write-Log 'Docker Desktop process already present.'
}

# 2. Wait for the Docker engine to accept commands (poll docker info)
$maxWaitSec = 300
$elapsed = 0
$ready = $false
$prevEAP2 = $ErrorActionPreference
$ErrorActionPreference = 'Continue'   # docker info failures are expected while waiting
while ($elapsed -lt $maxWaitSec) {
    docker info 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $ready = $true
        break
    }
    Start-Sleep -Seconds 5
    $elapsed += 5
}
$ErrorActionPreference = $prevEAP2

if (-not $ready) {
    Write-Log "ERROR: Docker engine not ready after ${maxWaitSec}s. Aborting."
    exit 1
}
Write-Log "Docker engine ready after ${elapsed}s."

# 3. Bring up the stack (idempotent)
Set-Location $ProjectDir
Write-Log 'Running: docker compose up -d'
# Docker writes progress lines ("Container X Running") to stderr even on
# success. Temporarily relax ErrorActionPreference so those benign stderr
# writes don't trip the $ErrorActionPreference='Stop' wrapper. We judge
# success by $LASTEXITCODE (the real docker exit code), not by stderr.
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$composeOut = docker compose up -d 2>&1 | Out-String
$composeExit = $LASTEXITCODE
$ErrorActionPreference = $prevEAP
Add-Content -Path $LogFile -Value $composeOut

if ($composeExit -ne 0) {
    Write-Log "ERROR: docker compose up -d exited $composeExit"
    exit 1
}

# 4. Verify daemon health endpoint
Start-Sleep -Seconds 30
try {
    $health = Invoke-RestMethod -Uri 'http://localhost:9079/health' -TimeoutSec 10
    Write-Log "Daemon health: $($health | ConvertTo-Json -Compress)"
} catch {
    Write-Log "WARN: health check failed: $($_.Exception.Message)"
}

Write-Log '=== musicstream boot autostart complete ==='
