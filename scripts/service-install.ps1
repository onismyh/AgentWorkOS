#Requires -Version 5.1
<#
.SYNOPSIS
    Register AgentWorkOS as per-user background tasks in Windows Task Scheduler (idempotent; re-run to update).

.DESCRIPTION
    \AgentWorkOS\Daemon    at logon of the current user (+30 s delay), and started right after registration:
                           .venv\Scripts\pythonw.exe -X utf8 -m awos.cli daemon   (working dir = repo root)
                           no execution time limit, restart on failure every 1 min (999x), IgnoreNew,
                           runs on battery, hidden (pythonw has no window).
    \AgentWorkOS\Watchdog  every 10 minutes, indefinitely: pythonw.exe -X utf8 -m awos.cli ensure-running

    Both run as the current user, only while that user is logged on (Interactive), non-elevated.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\service-install.ps1 -DryRun
.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\service-install.ps1
#>
[CmdletBinding()]
param(
    [Alias('WhatIf')][switch]$DryRun,   # print what would be registered; change nothing
    [switch]$NoStart,                   # register only; do not start the daemon now
    [ValidateRange(1, 1440)][int]$WatchdogMinutes = 10
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$PythonW = Join-Path $Root '.venv\Scripts\pythonw.exe'
$TaskPath = '\AgentWorkOS\'
$User = [Security.Principal.WindowsIdentity]::GetCurrent().Name   # DOMAIN\user

if (-not (Test-Path -LiteralPath $PythonW)) {
    if ($DryRun) { Write-Warning "$PythonW not found (run scripts\install.ps1 first)" }
    else { throw "$PythonW not found - run scripts\install.ps1 first" }
}

$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$common = @{
    AllowStartIfOnBatteries    = $true
    DontStopIfGoingOnBatteries = $true
    StartWhenAvailable         = $true
    MultipleInstances          = 'IgnoreNew'
    Hidden                     = $true
}
$exe = '"{0}"' -f $PythonW   # path has spaces / non-ASCII characters

$logon = New-ScheduledTaskTrigger -AtLogOn -User $User
$logon.Delay = 'PT30S'       # let the network / proxy come up first
$daemon = @{
    Name        = 'Daemon'
    Description = 'AgentWorkOS daemon: scheduler, workers and local API (127.0.0.1). Starts at logon, restarts on failure.'
    Action      = New-ScheduledTaskAction -Execute $exe -Argument '-X utf8 -m awos.cli daemon' -WorkingDirectory $Root
    Trigger     = @($logon)
    Settings    = New-ScheduledTaskSettingsSet @common -ExecutionTimeLimit ([TimeSpan]::Zero) `
                    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
}

# -Once + -RepetitionInterval without -RepetitionDuration = repeat indefinitely (Win10+).
$every = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(2)) -RepetitionInterval (New-TimeSpan -Minutes $WatchdogMinutes)
$watchdog = @{
    Name        = 'Watchdog'
    Description = "AgentWorkOS watchdog: every $WatchdogMinutes min, start the daemon if it is not running."
    Action      = New-ScheduledTaskAction -Execute $exe -Argument '-X utf8 -m awos.cli ensure-running' -WorkingDirectory $Root
    Trigger     = @($every)
    Settings    = New-ScheduledTaskSettingsSet @common -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
}

function Format-Trigger($t) {
    $kind = $t.CimClass.CimClassName -replace '^MSFT_Task', '' -replace 'Trigger$', ''
    $txt = $kind
    if ($t.PSObject.Properties['UserId'] -and $t.UserId) { $txt += " user=$($t.UserId)" }
    if ($t.PSObject.Properties['Delay'] -and $t.Delay) { $txt += " delay=$($t.Delay)" }
    if ($t.StartBoundary) { $txt += " start=$($t.StartBoundary)" }
    if ($t.Repetition -and $t.Repetition.Interval) {
        $for = if ($t.Repetition.Duration) { $t.Repetition.Duration } else { 'indefinitely' }
        $txt += " repeat-every=$($t.Repetition.Interval) for=$for"
    }
    return $txt
}

function Show-Task($t) {
    $a = $t.Action; $s = $t.Settings
    Write-Host "[dry-run] would register $TaskPath$($t.Name)" -ForegroundColor Yellow
    Write-Host "    Execute           : $($a.Execute)"
    Write-Host "    Arguments         : $($a.Arguments)"
    Write-Host "    WorkingDirectory  : $($a.WorkingDirectory)"
    Write-Host "    Principal         : $($principal.UserId) LogonType=$($principal.LogonType) RunLevel=$($principal.RunLevel)"
    foreach ($tr in $t.Trigger) { Write-Host "    Trigger           : $(Format-Trigger $tr)" }
    Write-Host ("    Settings          : ExecutionTimeLimit={0} RestartCount={1} RestartInterval={2} MultipleInstances={3}" -f `
            $s.ExecutionTimeLimit, $s.RestartCount, $s.RestartInterval, $s.MultipleInstances)
    Write-Host ("                        DisallowStartIfOnBatteries={0} StopIfGoingOnBatteries={1} StartWhenAvailable={2} Hidden={3}" -f `
            $s.DisallowStartIfOnBatteries, $s.StopIfGoingOnBatteries, $s.StartWhenAvailable, $s.Hidden)
    Write-Host "    Description       : $($t.Description)"
}

function Register-AwosTask($t) {
    try {
        Register-ScheduledTask -TaskName $t.Name -TaskPath $TaskPath -Action $t.Action -Trigger $t.Trigger `
            -Settings $t.Settings -Principal $principal -Description $t.Description -Force | Out-Null
    } catch {
        if ($_.Exception.Message -match 'Access is denied|0x80070005' -or $_.Exception.HResult -eq -2147024891) {
            Write-Warning 'Access denied. Run this script once from an elevated PowerShell (the tasks still run as you, non-elevated).'
        }
        throw
    }
    Write-Host "registered $TaskPath$($t.Name)" -ForegroundColor Green
}

Write-Host "AgentWorkOS root: $Root"
if ($DryRun) {
    Show-Task $daemon
    Show-Task $watchdog
    if (-not $NoStart) { Write-Host "[dry-run] would then run: Start-ScheduledTask -TaskPath '$TaskPath' -TaskName 'Daemon'" -ForegroundColor Yellow }
    $existing = @(Get-ScheduledTask -TaskPath $TaskPath -ErrorAction SilentlyContinue)
    Write-Host "currently registered under ${TaskPath}: $(if ($existing.Count) { ($existing | ForEach-Object { "$($_.TaskName) [$($_.State)]" }) -join ', ' } else { 'none' })"
    return
}

Register-AwosTask $daemon
Register-AwosTask $watchdog
if (-not $NoStart) {
    Start-ScheduledTask -TaskPath $TaskPath -TaskName 'Daemon'
    Write-Host 'started \AgentWorkOS\Daemon' -ForegroundColor Green
}
Get-ScheduledTask -TaskPath $TaskPath | ForEach-Object {
    $info = $_ | Get-ScheduledTaskInfo
    Write-Host ("    {0,-9} state={1,-8} lastRun={2} lastResult=0x{3:X} nextRun={4}" -f $_.TaskName, $_.State, $info.LastRunTime, $info.LastTaskResult, $info.NextRunTime)
}
Write-Host "check: $(Join-Path $Root 'scripts\awos.cmd') status    remove: scripts\service-uninstall.ps1"
