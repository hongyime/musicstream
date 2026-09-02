#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$InstallTask,
    [switch]$UninstallTask,
    [switch]$Once,
    [switch]$Loop,
    [int]$DockerWaitSeconds = 300,
    [int]$HealthWaitSeconds = 180,
    [int]$RestartCooldownMinutes = 20,
    [int]$LoopIntervalSeconds = 300,
    [int]$RepeatMinutes = 5
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$Script:Root = Split-Path -Parent $PSScriptRoot
$Script:LogDir = Join-Path $Script:Root "logs"
$Script:LogPath = Join-Path $Script:LogDir "self_heal.log"
$Script:StatePath = Join-Path $Script:LogDir "self_heal_state.json"
$Script:LastPath = Join-Path $Script:LogDir "self_heal_last.json"
$Script:EnvPath = Join-Path $Script:Root ".env"
$Script:TaskName = "SelfHeal"
$Script:TaskPath = "\Musicstream\"

$Script:Services = @(
    @{ Name = "postgres";  Container = "musicstream-postgres"  },
    @{ Name = "plex";      Container = "musicstream-plex"      },
    @{ Name = "scrobbler"; Container = "musicstream-scrobbler" },
    @{ Name = "daemon";    Container = "musicstream-daemon"    }
)

function Initialize-LogDir {
    if (-not (Test-Path -LiteralPath $Script:LogDir)) {
        New-Item -ItemType Directory -Path $Script:LogDir -Force | Out-Null
    }
}

function Write-Log {
    param(
        [Parameter(Mandatory = $true)][string]$Level,
        [Parameter(Mandatory = $true)][string]$Message
    )

    Initialize-LogDir
    $line = "{0} [{1}] {2}" -f (Get-Date).ToString("yyyy-MM-dd HH:mm:ss"), $Level.ToUpperInvariant(), $Message
    Add-Content -LiteralPath $Script:LogPath -Value $line
    Write-Host $line
}

function Invoke-External {
    param(
        [Parameter(Mandatory = $true)][string]$File,
        [string[]]$Arguments = @(),
        [switch]$NoThrow
    )

    $oldErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $File @Arguments 2>&1
        $exitCode = $LASTEXITCODE
        if ($null -eq $exitCode) {
            $exitCode = 0
        }
    } finally {
        $ErrorActionPreference = $oldErrorActionPreference
    }

    $text = ($output | ForEach-Object { $_.ToString() }) -join "`n"
    if ($exitCode -ne 0 -and -not $NoThrow) {
        throw ("{0} {1} failed with exit {2}: {3}" -f $File, ($Arguments -join " "), $exitCode, $text)
    }

    [pscustomobject]@{
        ExitCode = $exitCode
        Output = $text
    }
}

function Invoke-Docker {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$NoThrow
    )

    if ($NoThrow) {
        return Invoke-External -File "docker" -Arguments $Arguments -NoThrow
    }
    return Invoke-External -File "docker" -Arguments $Arguments
}

function Invoke-Compose {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$NoThrow
    )

    Push-Location $Script:Root
    try {
        $composeArgs = @("compose") + $Arguments
        if ($NoThrow) {
            return Invoke-Docker -Arguments $composeArgs -NoThrow
        }
        return Invoke-Docker -Arguments $composeArgs
    } finally {
        Pop-Location
    }
}

function Wait-DockerReady {
    param([int]$TimeoutSeconds)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $startedDockerDesktop = $false

    while ((Get-Date) -lt $deadline) {
        $result = Invoke-Docker -Arguments @("info") -NoThrow
        if ($result.ExitCode -eq 0) {
            Write-Log "info" "Docker engine is ready."
            return
        }

        if (-not $startedDockerDesktop) {
            $dockerDesktop = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
            if (Test-Path -LiteralPath $dockerDesktop) {
                Write-Log "info" "Docker engine is not ready; starting Docker Desktop."
                Start-Process -FilePath $dockerDesktop -WindowStyle Hidden
                $startedDockerDesktop = $true
            } else {
                Write-Log "warn" "Docker engine is not ready and Docker Desktop executable was not found."
                $startedDockerDesktop = $true
            }
        }

        Start-Sleep -Seconds 5
    }

    throw "Docker engine did not become ready within $TimeoutSeconds seconds."
}

function Get-EnvFileValue {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [string]$Default = ""
    )

    if (-not (Test-Path -LiteralPath $Script:EnvPath)) {
        return $Default
    }

    $escaped = [regex]::Escape($Name)
    foreach ($line in [System.IO.File]::ReadLines($Script:EnvPath)) {
        if ($line -match "^\s*#") {
            continue
        }
        if ($line -match "^\s*$escaped\s*=(.*)$") {
            $value = $Matches[1].Trim()
            if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            return $value
        }
    }

    return $Default
}

function Set-EnvFileValue {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Value
    )

    $lines = New-Object System.Collections.Generic.List[string]
    if (Test-Path -LiteralPath $Script:EnvPath) {
        foreach ($line in Get-Content -LiteralPath $Script:EnvPath) {
            [void]$lines.Add($line)
        }
    }

    $escaped = [regex]::Escape($Name)
    $replaced = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -notmatch "^\s*#" -and $lines[$i] -match "^\s*$escaped\s*=") {
            $lines[$i] = "$Name=$Value"
            $replaced = $true
            break
        }
    }

    if (-not $replaced) {
        [void]$lines.Add("$Name=$Value")
    }

    $encoding = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllLines($Script:EnvPath, $lines, $encoding)
}

function Convert-ToPort {
    param([string]$Value)

    $port = 0
    if ([int]::TryParse($Value, [ref]$port) -and $port -gt 0 -and $port -lt 65536) {
        return $port
    }
    return $null
}

function Test-TruthyFlag {
    param([string]$Value)

    return $Value -match "^(1|true|yes|on)$"
}

function Test-PlexHostPortAutoFallbackEnabled {
    if (Test-TruthyFlag -Value $env:PLEX_HOST_PORT_AUTO_FALLBACK) {
        return $true
    }
    return Test-TruthyFlag -Value (Get-EnvFileValue -Name "PLEX_HOST_PORT_AUTO_FALLBACK" -Default "false")
}

function Get-PreferredPlexPort {
    $fromProcess = Convert-ToPort $env:PLEX_HOST_PORT
    if ($null -ne $fromProcess) {
        return $fromProcess
    }

    $fromEnvFile = Convert-ToPort (Get-EnvFileValue -Name "PLEX_HOST_PORT" -Default "")
    if ($null -ne $fromEnvFile) {
        return $fromEnvFile
    }

    return 32401
}

function Get-CurrentPlexPublishedPort {
    $result = Invoke-Docker -Arguments @("port", "musicstream-plex", "32400/tcp") -NoThrow
    if ($result.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($result.Output)) {
        return $null
    }

    foreach ($line in ($result.Output -split "`n")) {
        if ($line.Trim() -match ":(\d+)$") {
            return [int]$Matches[1]
        }
    }

    return $null
}

function Get-PortOwnerDetails {
    param([int]$Port)

    $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($null -eq $connections) {
        return @()
    }

    $owners = @()
    foreach ($connection in $connections) {
        $processName = ""
        $processPath = ""
        $process = Get-Process -Id $connection.OwningProcess -ErrorAction SilentlyContinue
        if ($null -ne $process) {
            $processName = $process.ProcessName
            try {
                $processPath = [string]$process.Path
            } catch {
                $processPath = ""
            }
        }
        $owners += [pscustomobject]@{
            port = $Port
            pid = [int]$connection.OwningProcess
            process_name = $processName
            path = $processPath
            state = [string]$connection.State
        }
    }
    return $owners | Sort-Object -Property pid, process_name -Unique
}

function Format-PortOwnerDetails {
    param($Owners)

    $items = @()
    foreach ($owner in @($Owners)) {
        $items += ("{0}/{1}/{2}" -f $owner.pid, $owner.process_name, $owner.state)
    }
    return ($items -join ", ")
}

function Test-PlexHostPortPreflight {
    param([int]$Port)

    $currentPlexPort = Get-CurrentPlexPublishedPort
    $owners = @(Get-PortOwnerDetails -Port $Port)
    if ($null -ne $currentPlexPort -and $currentPlexPort -eq $Port) {
        return [pscustomobject]@{
            port = $Port
            usable = $true
            reason = "current-musicstream-plex"
            owners = $owners
            message = "Port $Port is already published by musicstream-plex."
        }
    }

    if ($owners.Count -eq 0) {
        return [pscustomobject]@{
            port = $Port
            usable = $true
            reason = "free"
            owners = @()
            message = "Port $Port is free."
        }
    }

    $names = @($owners | ForEach-Object { ([string]$_.process_name).ToLowerInvariant() })
    $reason = "other-process"
    if ($names | Where-Object { $_ -in @("com.docker.backend", "wslrelay") }) {
        $reason = "docker-backend-ghost"
    } elseif ($names | Where-Object { $_ -match "plex" }) {
        $reason = "host-plex"
    }

    $ownerText = Format-PortOwnerDetails -Owners $owners
    return [pscustomobject]@{
        port = $Port
        usable = $false
        reason = $reason
        owners = $owners
        message = "Port $Port is blocked by $reason owners: $ownerText"
    }
}

function Select-PlexHostPort {
    param([int[]]$Exclude = @())

    $preferred = Get-PreferredPlexPort
    $autoFallback = Test-PlexHostPortAutoFallbackEnabled
    $candidates = New-Object System.Collections.Generic.List[int]
    [void]$candidates.Add($preferred)
    foreach ($port in 32401..32410) {
        [void]$candidates.Add($port)
    }
    [void]$candidates.Add(32400)

    foreach ($port in ($candidates | Select-Object -Unique)) {
        if ($Exclude -contains $port) {
            if ($port -eq $preferred -and -not $autoFallback) {
                throw "Plex host port $port failed a compose bind and automatic fallback is disabled. Set PLEX_HOST_PORT manually or set PLEX_HOST_PORT_AUTO_FALLBACK=true."
            }
            continue
        }
        $decision = Test-PlexHostPortPreflight -Port $port
        if ($decision.usable) {
            if ($port -ne $preferred) {
                Write-Log "warn" ("Using fallback Plex host port {0}; preferred port {1} was unavailable." -f $port, $preferred)
            }
            return $port
        }

        Write-Log "warn" $decision.message
        if ($port -eq $preferred -and -not $autoFallback) {
            throw ("Plex host port {0} is unavailable ({1}) and automatic fallback is disabled. Set PLEX_HOST_PORT manually or set PLEX_HOST_PORT_AUTO_FALLBACK=true." -f $port, $decision.reason)
        }
    }

    throw "No usable Plex host port found in candidate range 32401-32410 plus 32400."
}

function Ensure-PlexHostPort {
    param([int[]]$Exclude = @())

    $port = Select-PlexHostPort -Exclude $Exclude
    $currentValue = Get-EnvFileValue -Name "PLEX_HOST_PORT" -Default ""
    if ($currentValue -ne "$port") {
        if (Test-PlexHostPortAutoFallbackEnabled) {
            Set-EnvFileValue -Name "PLEX_HOST_PORT" -Value "$port"
            Write-Log "info" "Persisted PLEX_HOST_PORT=$port in .env."
        } else {
            Write-Log "info" "Using PLEX_HOST_PORT=$port for this run without rewriting .env."
        }
    }
    $env:PLEX_HOST_PORT = "$port"
    return $port
}

function Start-ComposeStack {
    $excluded = @()

    for ($attempt = 1; $attempt -le 12; $attempt++) {
        $port = Ensure-PlexHostPort -Exclude $excluded
        Write-Log "info" "Starting musicstream Compose project with Plex host port $port."
        $result = Invoke-Compose -Arguments @("up", "-d") -NoThrow
        if ($result.ExitCode -eq 0) {
            return $port
        }

        $bindFailure = $result.Output -match "ports are not available|Only one usage|bind|port is already allocated"
        if ($bindFailure) {
            Write-Log "warn" ("Compose bind failed on Plex host port {0}: {1}" -f $port, ($result.Output -replace "\s+", " ").Trim())
            if (-not (Test-PlexHostPortAutoFallbackEnabled)) {
                throw "Compose bind failed on Plex host port $port and automatic fallback is disabled. Set PLEX_HOST_PORT manually or set PLEX_HOST_PORT_AUTO_FALLBACK=true."
            }
            $excluded += $port
            continue
        }

        throw ("docker compose up failed: {0}" -f $result.Output)
    }

    throw "Unable to start Compose after trying Plex fallback ports."
}

function Get-ContainerState {
    param([Parameter(Mandatory = $true)][string]$Container)

    $format = "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"
    $result = Invoke-Docker -Arguments @("inspect", "--format", $format, $Container) -NoThrow
    if ($result.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($result.Output)) {
        return [pscustomobject]@{ Status = "missing"; Health = "missing" }
    }

    $parts = $result.Output.Trim() -split "\|", 2
    $health = "none"
    if ($parts.Count -gt 1) {
        $health = $parts[1]
    }

    [pscustomobject]@{
        Status = $parts[0]
        Health = $health
    }
}

function Read-State {
    if (-not (Test-Path -LiteralPath $Script:StatePath)) {
        return @{}
    }

    try {
        $json = Get-Content -LiteralPath $Script:StatePath -Raw
        if ([string]::IsNullOrWhiteSpace($json)) {
            return @{}
        }
        $parsed = ConvertFrom-Json -InputObject $json
        $state = @{}
        if ($null -eq $parsed) {
            return $state
        }
        foreach ($property in $parsed.PSObject.Properties) {
            $state[$property.Name] = [string]$property.Value
        }
        return $state
    } catch {
        return @{}
    }
}

function Write-State {
    param([hashtable]$State)

    Initialize-LogDir
    $State | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $Script:StatePath -Encoding UTF8
}

function Test-CooldownReady {
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [int]$Minutes
    )

    $state = Read-State
    if (-not $state.ContainsKey($Key)) {
        return $true
    }

    try {
        $last = [datetime]::Parse($state[$Key])
        return ((Get-Date) - $last).TotalMinutes -ge $Minutes
    } catch {
        return $true
    }
}

function Mark-Action {
    param([Parameter(Mandatory = $true)][string]$Key)

    $state = Read-State
    $state[$Key] = (Get-Date).ToString("o")
    Write-State -State $state
}

function Restart-ContainerWithCooldown {
    param(
        [Parameter(Mandatory = $true)][string]$Container,
        [Parameter(Mandatory = $true)][string]$Reason
    )

    $key = "restart:${Container}:${Reason}"
    if (-not (Test-CooldownReady -Key $key -Minutes $RestartCooldownMinutes)) {
        Write-Log "warn" "Skipping restart of $Container for $Reason; cooldown is active."
        return
    }

    Write-Log "warn" "Restarting $Container because $Reason."
    $result = Invoke-Docker -Arguments @("restart", $Container) -NoThrow
    if ($result.ExitCode -eq 0) {
        Mark-Action -Key $key
        return
    }

    Write-Log "error" ("docker restart {0} failed: {1}" -f $Container, $result.Output)
}

function Test-PlexMaintenance {
    $result = Invoke-Docker -Arguments @(
        "exec", "musicstream-plex",
        "sh", "-lc", "curl -s --max-time 5 http://localhost:32400/identity || true"
    ) -NoThrow

    return ($result.Output -match "database migrations|Maintenance")
}

function Repair-Containers {
    foreach ($service in $Script:Services) {
        $name = [string]$service.Name
        $container = [string]$service.Container
        $state = Get-ContainerState -Container $container

        if ($state.Status -in @("missing", "created", "exited", "dead")) {
            Write-Log "warn" "Container $container is $($state.Status); running docker compose up -d $name."
            $result = Invoke-Compose -Arguments @("up", "-d", $name) -NoThrow
            if ($result.ExitCode -ne 0) {
                Write-Log "error" ("docker compose up -d {0} failed: {1}" -f $name, $result.Output)
            }
            continue
        }

        if ($state.Health -eq "unhealthy") {
            if ($container -eq "musicstream-plex" -and (Test-PlexMaintenance)) {
                Write-Log "info" "Plex is unhealthy because it is running database migrations; waiting instead of restarting it."
                continue
            }
            Restart-ContainerWithCooldown -Container $container -Reason "unhealthy"
        }
    }
}

function Test-HttpOk {
    param([Parameter(Mandatory = $true)][string]$Uri)

    try {
        Invoke-WebRequest -Uri $Uri -TimeoutSec 10 -UseBasicParsing | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Get-JsonPropertyValue {
    param(
        $Object,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if ($null -eq $Object) {
        return $null
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $null
    }
    return $property.Value
}

function Convert-ToNullableBool {
    param($Value)

    if ($null -eq $Value) {
        return $null
    }
    return [bool]$Value
}

function Get-DeepHealthSnapshot {
    $uri = "http://127.0.0.1:9079/health/deep"
    $statusCode = 0
    $content = ""

    try {
        $response = Invoke-WebRequest -Uri $uri -TimeoutSec 10 -UseBasicParsing
        $statusCode = [int]$response.StatusCode
        $content = [string]$response.Content
    } catch {
        $response = $_.Exception.Response
        if (-not [string]::IsNullOrWhiteSpace($_.ErrorDetails.Message)) {
            $content = [string]$_.ErrorDetails.Message
        }
        if ($null -eq $response) {
            Write-Log "warn" ("Deep health request failed: {0}" -f $_.Exception.Message)
            return [pscustomobject]@{
                reachable = $false
                status_code = 0
                status = "unreachable"
                db = $null
                scheduler_running = $null
                last_run_started_at = $null
                last_run_age_seconds = $null
                last_run_fresh = $null
                error = $_.Exception.Message
            }
        }

        try {
            $statusCode = [int]$response.StatusCode
            $stream = $response.GetResponseStream()
            if ([string]::IsNullOrWhiteSpace($content) -and $null -ne $stream) {
                $reader = New-Object System.IO.StreamReader($stream)
                try {
                    $content = $reader.ReadToEnd()
                } finally {
                    $reader.Dispose()
                }
            }
        } catch {
            Write-Log "warn" ("Could not read deep health error body: {0}" -f $_.Exception.Message)
        }
    }

    $payload = $null
    if (-not [string]::IsNullOrWhiteSpace($content)) {
        try {
            $payload = ConvertFrom-Json -InputObject $content
        } catch {
            Write-Log "warn" ("Deep health returned non-JSON response: {0}" -f ($content -replace "\s+", " ").Trim())
        }
    }

    if ($null -eq $payload) {
        return [pscustomobject]@{
            reachable = $true
            status_code = $statusCode
            status = "unknown"
            db = $null
            scheduler_running = $null
            last_run_started_at = $null
            last_run_age_seconds = $null
            last_run_fresh = $null
            error = "unparseable deep health response"
        }
    }

    $ageValue = Get-JsonPropertyValue -Object $payload -Name "last_run_age_seconds"
    $age = $null
    if ($null -ne $ageValue) {
        $age = [int64]$ageValue
    }

    [pscustomobject]@{
        reachable = $true
        status_code = $statusCode
        status = [string](Get-JsonPropertyValue -Object $payload -Name "status")
        db = Convert-ToNullableBool (Get-JsonPropertyValue -Object $payload -Name "db")
        scheduler_running = Convert-ToNullableBool (Get-JsonPropertyValue -Object $payload -Name "scheduler_running")
        last_run_started_at = Get-JsonPropertyValue -Object $payload -Name "last_run_started_at"
        last_run_age_seconds = $age
        last_run_fresh = Convert-ToNullableBool (Get-JsonPropertyValue -Object $payload -Name "last_run_fresh")
        error = ""
    }
}

function Wait-ServiceHealth {
    param([int]$PlexPort, [int]$TimeoutSeconds)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $bad = @()
        foreach ($service in $Script:Services) {
            $state = Get-ContainerState -Container ([string]$service.Container)
            if ($state.Status -ne "running") {
                $bad += ("{0}:{1}" -f $service.Container, $state.Status)
                continue
            }
            if ($state.Health -eq "unhealthy") {
                $bad += ("{0}:unhealthy" -f $service.Container)
            }
        }

        if ($bad.Count -eq 0 -and
            (Test-HttpOk -Uri "http://127.0.0.1:9079/health") -and
            (Test-HttpOk -Uri "http://127.0.0.1:$PlexPort/identity")) {
            Write-Log "info" "Musicstream health checks are passing."
            return $true
        }

        Start-Sleep -Seconds 5
    }

    Write-Log "warn" "Timed out waiting for full health; leaving watchdog state for the next scheduled pass."
    return $false
}

function Get-ProgressSnapshot {
    $sql = @"
WITH counts AS (
    SELECT
        COUNT(*) AS total,
        COUNT(*) FILTER (WHERE status = 'downloaded') AS downloaded,
        COUNT(*) FILTER (WHERE status = 'pending') AS pending,
        COUNT(*) FILTER (WHERE status = 'downloading') AS downloading,
        COUNT(*) FILTER (
            WHERE status = 'downloading'
              AND COALESCE(heartbeat_at, updated_at) < now() - interval '30 minutes'
              AND file_path IS NULL
        ) AS stale_downloading
    FROM tracks
),
attempts AS (
    SELECT
        COUNT(*) FILTER (WHERE success AND attempted_at >= now() - interval '1 hour') AS success_1h,
        COUNT(*) FILTER (WHERE success AND attempted_at >= now() - interval '24 hours') AS success_24h,
        MAX(attempted_at) FILTER (WHERE success) AS last_success_at,
        MAX(attempted_at) AS last_attempt_at
    FROM download_attempts
)
SELECT
    total,
    downloaded,
    pending,
    downloading,
    stale_downloading,
    success_1h,
    success_24h,
    COALESCE(last_success_at::text, ''),
    COALESCE(last_attempt_at::text, '')
FROM counts, attempts;
"@

    $result = Invoke-Docker -Arguments @(
        "exec", "musicstream-postgres",
        "psql", "-U", "musicstream", "-d", "musicstream",
        "-X", "-A", "-t", "-F", "|", "-c", $sql
    ) -NoThrow

    if ($result.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($result.Output)) {
        Write-Log "warn" ("Could not read progress snapshot: {0}" -f $result.Output)
        return $null
    }

    $parts = $result.Output.Trim() -split "\|"
    if ($parts.Count -lt 9) {
        Write-Log "warn" ("Unexpected progress snapshot format: {0}" -f $result.Output)
        return $null
    }

    [pscustomobject]@{
        total = [int]$parts[0]
        downloaded = [int]$parts[1]
        pending = [int]$parts[2]
        downloading = [int]$parts[3]
        stale_downloading = [int]$parts[4]
        success_1h = [int]$parts[5]
        success_24h = [int]$parts[6]
        last_success_at = $parts[7]
        last_attempt_at = $parts[8]
    }
}

function Repair-StaleDownloads {
    param([Parameter(Mandatory = $true)]$Snapshot)

    Write-Log "info" ("Progress: total={0} downloaded={1} pending={2} downloading={3} stale_downloading={4} success_1h={5} success_24h={6}" -f `
        $Snapshot.total, $Snapshot.downloaded, $Snapshot.pending, $Snapshot.downloading, $Snapshot.stale_downloading, $Snapshot.success_1h, $Snapshot.success_24h)

    if ($Snapshot.stale_downloading -gt 0) {
        Restart-ContainerWithCooldown -Container "musicstream-daemon" -Reason "stale-downloads"
    }
}

function Repair-DaemonDeepHealth {
    param($DeepHealth)

    if ($null -eq $DeepHealth) {
        return
    }

    $reasons = @()
    if (-not $DeepHealth.reachable) {
        $reasons += "deep-health-unreachable"
    } else {
        if ($DeepHealth.scheduler_running -eq $false) {
            $reasons += "scheduler-not-running"
        }
        if ($DeepHealth.last_run_fresh -eq $false) {
            $reasons += "last-run-stale"
        }
    }

    if ($reasons.Count -eq 0) {
        if ($DeepHealth.status -eq "ok") {
            Write-Log "info" "Deep health: scheduler running and latest daemon run is fresh."
        } else {
            Write-Log "warn" ("Deep health degraded without daemon-restart signal: status={0} db={1}" -f $DeepHealth.status, $DeepHealth.db)
        }
        return
    }

    Write-Log "warn" ("Deep health degraded: status={0} scheduler_running={1} last_run_fresh={2} last_run_age_seconds={3}; repair={4}" -f `
        $DeepHealth.status, $DeepHealth.scheduler_running, $DeepHealth.last_run_fresh, $DeepHealth.last_run_age_seconds, ($reasons -join "+"))
    Restart-ContainerWithCooldown -Container "musicstream-daemon" -Reason ($reasons -join "+")
}

function Write-LastSummary {
    param(
        [string]$Status,
        [int]$PlexPort,
        $Snapshot,
        $DeepHealth,
        [string]$ErrorMessage = ""
    )

    Initialize-LogDir
    [pscustomobject]@{
        checked_at = (Get-Date).ToString("o")
        status = $Status
        plex_host_port = $PlexPort
        snapshot = $Snapshot
        deep_health = $DeepHealth
        error = $ErrorMessage
    } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $Script:LastPath -Encoding UTF8
}

function Invoke-SelfHeal {
    $plexPort = 0
    $snapshot = $null
    $deepHealth = $null

    try {
        Write-Log "info" "Self-heal pass started."
        Wait-DockerReady -TimeoutSeconds $DockerWaitSeconds
        $plexPort = Start-ComposeStack
        Repair-Containers
        $healthy = Wait-ServiceHealth -PlexPort $plexPort -TimeoutSeconds $HealthWaitSeconds
        $deepHealth = Get-DeepHealthSnapshot
        Repair-DaemonDeepHealth -DeepHealth $deepHealth
        $snapshot = Get-ProgressSnapshot
        if ($null -ne $snapshot) {
            Repair-StaleDownloads -Snapshot $snapshot
        }
        $status = "degraded"
        if ($healthy -and $null -ne $deepHealth -and $deepHealth.status -eq "ok") {
            $status = "ok"
        }
        Write-LastSummary -Status $status -PlexPort $plexPort -Snapshot $snapshot -DeepHealth $deepHealth
        Write-Log "info" "Self-heal pass completed."
        return 0
    } catch {
        $message = $_.Exception.Message
        Write-Log "error" $message
        Write-LastSummary -Status "error" -PlexPort $plexPort -Snapshot $snapshot -DeepHealth $deepHealth -ErrorMessage $message
        return 1
    }
}

function Invoke-SelfHealLoop {
    $mutex = New-Object System.Threading.Mutex($false, "MusicstreamSelfHealLoop")
    $ownsMutex = $false

    try {
        $ownsMutex = $mutex.WaitOne(0)
        if (-not $ownsMutex) {
            Write-Log "warn" "Self-heal loop is already running; exiting duplicate launcher."
            return 0
        }

        Write-Log "info" "Self-heal loop started with ${LoopIntervalSeconds}s interval."
        while ($true) {
            [void](Invoke-SelfHeal)
            Start-Sleep -Seconds $LoopIntervalSeconds
        }
    } finally {
        if ($ownsMutex) {
            $mutex.ReleaseMutex()
        }
        $mutex.Dispose()
    }
}

function Get-PowerShellExecutable {
    $pwsh = Get-Command "pwsh.exe" -ErrorAction SilentlyContinue
    if ($null -ne $pwsh) {
        return $pwsh.Source
    }
    $powershell = Get-Command "powershell.exe" -ErrorAction Stop
    return $powershell.Source
}

function Install-StartupFallback {
    $startup = [Environment]::GetFolderPath("Startup")
    if ([string]::IsNullOrWhiteSpace($startup)) {
        throw "Could not resolve the current user's Startup folder."
    }

    $startupWrapper = Join-Path $Script:Root "scripts\Musicstream_Startup.bat"
    $loopWrapper = Join-Path $Script:Root "scripts\Musicstream_SelfHeal_Loop.cmd"

    Copy-Item -LiteralPath $startupWrapper -Destination (Join-Path $startup "Musicstream_Startup.bat") -Force
    Copy-Item -LiteralPath $loopWrapper -Destination (Join-Path $startup "Musicstream_SelfHeal_Loop.cmd") -Force
    Write-Log "info" "Installed Startup-folder self-heal wrappers in $startup."
}

function Register-SelfHealTask {
    $scriptPath = $PSCommandPath
    if ([string]::IsNullOrWhiteSpace($scriptPath)) {
        throw "Cannot register scheduled task because PSCommandPath is empty."
    }

    $psExe = Get-PowerShellExecutable
    $actionArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`" -Once"
    $action = New-ScheduledTaskAction -Execute $psExe -Argument $actionArgs -WorkingDirectory $Script:Root
    $repeatTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $RepeatMinutes) -RepetitionDuration (New-TimeSpan -Days 3650)
    $logonTrigger = New-ScheduledTaskTrigger -AtLogOn
    $startupTrigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
    $description = "Starts and repairs the Musicstream Docker Compose stack, including Plex host-port fallback."

    try {
        Register-ScheduledTask -TaskName $Script:TaskName -TaskPath $Script:TaskPath -Action $action -Trigger @($startupTrigger, $logonTrigger, $repeatTrigger) -Settings $settings -Description $description -Force | Out-Null
        Write-Log "info" "Registered scheduled task $($Script:TaskPath)$($Script:TaskName) with startup, logon, and $RepeatMinutes-minute triggers."
        return
    } catch {
        Write-Log "warn" ("Startup trigger registration failed; retrying with logon and repeating triggers only: {0}" -f $_.Exception.Message)
    }

    try {
        Register-ScheduledTask -TaskName $Script:TaskName -TaskPath $Script:TaskPath -Action $action -Trigger @($logonTrigger, $repeatTrigger) -Settings $settings -Description $description -Force | Out-Null
        Write-Log "info" "Registered scheduled task $($Script:TaskPath)$($Script:TaskName) with logon and $RepeatMinutes-minute triggers."
        return
    } catch {
        Write-Log "warn" ("Scheduled task registration is unavailable; installing Startup-folder fallback: {0}" -f $_.Exception.Message)
    }

    Install-StartupFallback
}

function Unregister-SelfHealTask {
    Unregister-ScheduledTask -TaskName $Script:TaskName -TaskPath $Script:TaskPath -Confirm:$false -ErrorAction Stop
    Write-Log "info" "Unregistered scheduled task $($Script:TaskPath)$($Script:TaskName)."
}

if ($UninstallTask) {
    Unregister-SelfHealTask
    exit 0
}

if ($InstallTask) {
    Register-SelfHealTask
    exit (Invoke-SelfHeal)
}

if ($Loop) {
    exit (Invoke-SelfHealLoop)
}

exit (Invoke-SelfHeal)
