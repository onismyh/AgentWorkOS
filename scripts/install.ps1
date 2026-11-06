#Requires -Version 5.1
<#
.SYNOPSIS
    AgentWorkOS bootstrap for a fresh Windows machine. Idempotent: safe to re-run at any time.

.DESCRIPTION
    1. checks uv (prints how to install it if missing)
    2. creates .venv with Python 3.13 (uv venv --python 3.13) unless it already exists
    3. uv pip install -e .[dev]
    4. creates the var\ sub-directories
    5. downloads a portable Node.js LTS (default v24, win-x64 zip) into runtime\node when runtime\node\node.exe
       is absent; SHA256 is verified against SHASUMS256.txt. The system Node is never touched.
       Downloads use the Windows system (IE) proxy, or -Proxy.
    6. optional: -AddToPath puts <root>\scripts on the user PATH (so `awos ...` works in new terminals)
    7. awos setup: codex / pi / dsh into runtime\ (pinned npm versions, no lifecycle scripts) and credentials
       already on this machine (Codex login, DEEPSEEK_API_KEY, DeepSeek Harness login) into var\secrets.toml
    8. optional: -RegisterService runs scripts\service-install.ps1 (Task Scheduler)

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1
.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1 -RegisterService -AddToPath
.NOTES
    -ForceNode deletes runtime\node first (including anything npm-installed into it) and downloads again.
#>
[CmdletBinding()]
param(
    [switch]$RegisterService,
    [switch]$AddToPath,
    [switch]$SkipNode,
    [switch]$ForceNode,
    [ValidateRange(22, 99)][int]$NodeMajor = 24,
    [string]$Proxy = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # the progress bar makes Invoke-WebRequest ~10x slower on PS 5.1
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$VenvPy = Join-Path $Root '.venv\Scripts\python.exe'
$NodeDir = Join-Path $Root 'runtime\node'
$NodeExe = Join-Path $NodeDir 'node.exe'
$MinNode = [version]'22.19.0'
$VarDirs = @('var', 'var\workspaces', 'var\artifacts', 'var\logs', 'var\runs', 'var\backups', 'var\context', 'var\inbox-drop')

function Write-Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "    $Text" -ForegroundColor Green }

function Invoke-Native {
    # Run a native exe; stream its output (stderr included, as text) and throw on a non-zero exit code.
    # EAP is relaxed inside: PS 5.1 turns redirected native stderr into error records, which 'Stop' would abort on.
    param([Parameter(Mandatory = $true)][string]$FilePath, [string[]]$ArgumentList = @())
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $FilePath @ArgumentList 2>&1 | ForEach-Object { Write-Host "    $_" }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($code -ne 0) { throw "'$FilePath $($ArgumentList -join ' ')' failed with exit code $code" }
}

function Get-ExeOutput([string]$FilePath, [string[]]$ArgumentList) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $out = (& $FilePath @ArgumentList 2>$null | Out-String).Trim() } finally { $ErrorActionPreference = $prev }
    if ($LASTEXITCODE -ne 0) { throw "'$FilePath $($ArgumentList -join ' ')' failed with exit code $LASTEXITCODE" }
    return $out
}

function Get-NodeVersion([string]$Exe) { [version]((Get-ExeOutput $Exe @('--version')).TrimStart('v')) }

function Install-PortableNode {
    $web = @{ UseBasicParsing = $true }
    if ($Proxy) { $web['Proxy'] = $Proxy }
    Write-Ok "resolving latest Node v$NodeMajor from https://nodejs.org/dist/index.json"
    $index = Invoke-RestMethod -Uri 'https://nodejs.org/dist/index.json' @web
    $candidates = @($index | Where-Object { $_.version -like "v$NodeMajor.*" -and (@($_.files) -contains 'win-x64-zip') } |
        Sort-Object -Property @{ Expression = { [version]($_.version.TrimStart('v')) } } -Descending)
    $lts = @($candidates | Where-Object { $_.lts })
    if ($lts.Count -gt 0) { $rel = $lts[0] }
    elseif ($candidates.Count -gt 0) { $rel = $candidates[0]; Write-Warning "no LTS build of v$NodeMajor yet; using $($rel.version)" }
    else { throw "no Node v$NodeMajor win-x64 zip listed in index.json" }

    $ver = $rel.version
    $zipName = "node-$ver-win-x64.zip"
    $base = "https://nodejs.org/dist/$ver"
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('awos-node-' + [guid]::NewGuid().ToString('N'))   # new, empty
    $stage = Join-Path $Root ('runtime\.node-staging-' + [guid]::NewGuid().ToString('N'))         # same volume as target
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        $zip = Join-Path $tmp $zipName
        $sums = Join-Path $tmp 'SHASUMS256.txt'
        Write-Ok "downloading $base/$zipName"
        Invoke-WebRequest -Uri "$base/$zipName" -OutFile $zip @web
        Invoke-WebRequest -Uri "$base/SHASUMS256.txt" -OutFile $sums @web

        $expected = $null
        foreach ($line in Get-Content -LiteralPath $sums) {
            if ($line -match '^([0-9a-fA-F]{64})\s+(\S+)\s*$' -and $Matches[2] -eq $zipName) { $expected = $Matches[1].ToLowerInvariant() }
        }
        if (-not $expected) { throw "$zipName is not listed in SHASUMS256.txt" }
        $actual = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $expected) { throw "SHA256 mismatch for ${zipName}: expected $expected, got $actual" }
        Write-Ok "SHA256 verified: $actual"

        Add-Type -AssemblyName System.IO.Compression.FileSystem
        New-Item -ItemType Directory -Path $stage | Out-Null
        [IO.Compression.ZipFile]::ExtractToDirectory($zip, $stage)   # .NET >= 4.6.2 rejects path traversal entries
        $inner = Join-Path $stage "node-$ver-win-x64"
        if (-not (Test-Path -LiteralPath (Join-Path $inner 'node.exe'))) { throw "unexpected archive layout in $zipName" }
        if (Test-Path -LiteralPath $NodeDir) { Remove-Item -LiteralPath $NodeDir -Recurse -Force }
        Move-Item -LiteralPath $inner -Destination $NodeDir
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

function Add-ScriptsToUserPath {
    $dir = Join-Path $Root 'scripts'
    $key = Get-Item -LiteralPath 'HKCU:\Environment'
    $raw = [string]$key.GetValue('Path', '', 'DoNotExpandEnvironmentNames')   # keep %VAR% entries unexpanded
    $parts = @($raw -split ';' | Where-Object { $_ })
    if ($parts -contains $dir) { Write-Ok "user PATH already contains $dir"; return }
    Set-ItemProperty -LiteralPath 'HKCU:\Environment' -Name 'Path' -Value (($parts + $dir) -join ';') -Type ExpandString
    # Broadcast WM_SETTINGCHANGE so new terminals see it (setting/clearing any user variable does that).
    [Environment]::SetEnvironmentVariable('AWOS_PATH_REFRESH', '1', 'User')
    [Environment]::SetEnvironmentVariable('AWOS_PATH_REFRESH', $null, 'User')
    Write-Ok "added $dir to the user PATH (open a new terminal)"
}

$prevOutEnc = [Console]::OutputEncoding
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }   # uv prints UTF-8
try {
    Write-Host "AgentWorkOS root: $Root"

    Write-Step 'uv'
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -eq $uv) {
        Write-Host '    uv is not installed. Install it, open a new terminal, then re-run this script:' -ForegroundColor Yellow
        Write-Host '      powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
        Write-Host '    (or: winget install --id=astral-sh.uv -e)'
        exit 1
    }
    Write-Ok ("{0} ({1})" -f (Get-ExeOutput $uv.Source @('--version')), $uv.Source)

    Write-Step 'Python virtualenv (.venv, Python 3.13)'
    if (Test-Path -LiteralPath $VenvPy) {
        $pyv = [version](Get-ExeOutput $VenvPy @('-c', 'import sys; print(sys.version.split()[0])'))
        if ($pyv.Major -ne 3 -or $pyv.Minor -ne 13) {
            Write-Warning ".venv uses Python $pyv (expected 3.13). Delete .venv and re-run to recreate it."
        } else { Write-Ok ".venv exists (Python $pyv) - kept" }
    } else {
        Invoke-Native $uv.Source @('venv', '--python', '3.13', (Join-Path $Root '.venv'))
    }

    Write-Step 'Python dependencies (uv pip install -e .[dev])'
    Push-Location -LiteralPath $Root
    try { Invoke-Native $uv.Source @('pip', 'install', '--python', $VenvPy, '-e', '.[dev]') } finally { Pop-Location }

    Write-Step 'data directories (var\)'
    foreach ($d in $VarDirs) { New-Item -ItemType Directory -Force -Path (Join-Path $Root $d) | Out-Null }
    Write-Ok ($VarDirs -join ', ')

    if ($SkipNode) {
        Write-Step 'portable Node.js: skipped (-SkipNode)'
    } else {
        Write-Step "portable Node.js v$NodeMajor LTS (runtime\node)"
        if ((Test-Path -LiteralPath $NodeExe) -and -not $ForceNode) {
            Write-Ok "already present - kept (use -ForceNode to re-download)"
        } else {
            Install-PortableNode
        }
        $nv = Get-NodeVersion $NodeExe
        if ($nv -lt $MinNode) { throw "portable Node $nv is older than the required $MinNode" }
        Write-Ok "runtime\node\node.exe = v$nv (>= $MinNode)"
        $sysNode = Get-Command node -ErrorAction SilentlyContinue
        if ($null -ne $sysNode -and $sysNode.Source -ne $NodeExe) {
            Write-Ok ("system Node untouched: {0} at {1}" -f (Get-ExeOutput $sysNode.Source @('--version')), $sysNode.Source)
        }
    }

    if (-not $SkipNode) {
        Write-Step 'agent runtimes + credentials (awos setup: codex / pi / dsh into runtime\, keys found on this machine)'
        Push-Location -LiteralPath $Root
        try { Invoke-Native $VenvPy @('-X', 'utf8', '-m', 'awos.cli', 'setup') }
        catch { Write-Warning "awos setup reported problems ($($_.Exception.Message)); re-run: scripts\awos.cmd setup" }
        finally { Pop-Location }
    }

    if ($AddToPath) { Write-Step 'user PATH'; Add-ScriptsToUserPath }

    if ($RegisterService) {
        Write-Step 'Task Scheduler registration'
        & (Join-Path $PSScriptRoot 'service-install.ps1')
    }

    Write-Step 'done'
    Write-Host "    CLI:       $(Join-Path $Root 'scripts\awos.cmd') status"
    Write-Host "    service:   powershell -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $PSScriptRoot 'service-install.ps1')`""
    Write-Host "    runbook:   $(Join-Path $Root 'docs\runbook.md')"
} finally {
    try { [Console]::OutputEncoding = $prevOutEnc } catch { }
}
