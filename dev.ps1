# Dev only: explicit build, then source edits use up --no-build.
param(
    [Parameter(Position = 0)]
    [ValidateSet('up', 'build', 'down', 'logs', 'ps')]
    [string]$Action = 'up',
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ComposeArgs = @()
)
$ErrorActionPreference = 'Stop'
if ($Action -eq 'up') {
    foreach ($argument in $ComposeArgs) {
        if ($argument -like '--build*' -or $argument -like '--no-build*' -or $argument -like '--pull*') {
            Write-Error 'Use the explicit build action for dependency changes; pulling is a separate command.'
            exit 2
        }
    }
}
$composeCommand = @('compose', '--env-file', '.env.dev', '-f', 'compose.dev.yaml', $Action)
if ($Action -eq 'up') { $composeCommand += '--no-build' }
$composeCommand += $ComposeArgs
Push-Location $PSScriptRoot
try {
    & docker @composeCommand
    $composeExit = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $composeExit
