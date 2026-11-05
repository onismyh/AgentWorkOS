#Requires -Version 5.1
<#
.SYNOPSIS
    Remove the AgentWorkOS Task Scheduler tasks (\AgentWorkOS\Watchdog, \AgentWorkOS\Daemon). Idempotent.

.DESCRIPTION
    1. stops + unregisters the Watchdog first (so it cannot restart the daemon)
    2. asks the daemon to exit gracefully (python -m awos.cli stop) unless -KeepDaemonRunning
    3. stops + unregisters the Daemon task, then removes the empty \AgentWorkOS\ folder
    Data in var\ is never touched.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\service-uninstall.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [Alias('WhatIf')][switch]$DryRun,
    [switch]$KeepDaemonRunning
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$TaskPath = '\AgentWorkOS\'

function Remove-AwosTask([string]$Name) {
    $task = Get-ScheduledTask -TaskPath $TaskPath -TaskName $Name -ErrorAction SilentlyContinue
    if ($null -eq $task) { Write-Host "    $TaskPath$Name is not registered"; return }
    if ($DryRun) { Write-Host "[dry-run] would stop and unregister $TaskPath$Name (state: $($task.State))" -ForegroundColor Yellow; return }
    if ($task.State -eq 'Running') { Stop-ScheduledTask -TaskPath $TaskPath -TaskName $Name }
    Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $Name -Confirm:$false
    Write-Host "unregistered $TaskPath$Name" -ForegroundColor Green
}

function Stop-AwosDaemon {
    if (-not (Test-Path -LiteralPath $Python)) { Write-Warning "$Python not found; skipping graceful stop"; return }
    if ($DryRun) { Write-Host "[dry-run] would run: `"$Python`" -X utf8 -m awos.cli stop" -ForegroundColor Yellow; return }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'   # native stderr must not abort the script
    try {
        Push-Location -LiteralPath $Root
        & $Python -X utf8 -m awos.cli stop 2>&1 | ForEach-Object { Write-Host "    $_" }
        if ($LASTEXITCODE -ne 0) { Write-Warning "awos stop exited with $LASTEXITCODE (daemon may not be running)" }
    } finally {
        Pop-Location
        $ErrorActionPreference = $prev
    }
}

Write-Host "AgentWorkOS root: $Root"
Remove-AwosTask 'Watchdog'
if (-not $KeepDaemonRunning) { Stop-AwosDaemon }
Remove-AwosTask 'Daemon'

if (-not $DryRun) {
    try {
        $svc = New-Object -ComObject 'Schedule.Service'
        $svc.Connect()
        $folder = $svc.GetFolder($TaskPath.TrimEnd('\'))
        if ($folder.GetTasks(1).Count -eq 0 -and $folder.GetFolders(0).Count -eq 0) {
            $svc.GetFolder('\').DeleteFolder('AgentWorkOS', 0)
            Write-Host "removed empty folder $TaskPath" -ForegroundColor Green
        }
    } catch { }   # folder absent or not empty: nothing to do
}
Write-Host 'data in var\ was left untouched.'
