# tools/bootstrap.ps1
# Unified bootstrap for Windows PowerShell 5.1+

param(
    [string]$JoinCode = "",
    [string]$Role = "",
    [ValidateSet("auto", "always", "never")]
    [string]$InstallService = "auto",
    [string]$ServeHost = "127.0.0.1",
    [int]$ServePort = 8777,
    [int]$ControlPort = 8777,
    [string]$RootUrl = "https://api.inimatic.com",
    [string]$Rev = "rev2026"
)

$ErrorActionPreference = "Stop"
$subPath = "src\adaos\integrations\inimatic"

# Ensure we operate from repo root even if invoked from elsewhere.
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot
function Get-PythonCandidates {
    $cands = @()

    if (Get-Command py -ErrorAction SilentlyContinue) {
        $lines = & py -0p 2>$null
        foreach ($ln in $lines) {
            if ($ln -match "(?<path>[A-Za-z]:\\.+?python\.exe)\s*$") {
                $path = $Matches["path"]
                try {
                    $out = & "$path" -c "import sys,platform; print(f'{sys.version_info[0]}.{sys.version_info[1]}|{platform.architecture()[0]}')" 2>$null
                    $ver,$arch = $out.Split("|")
                    $v = [version]$ver
                    $cands += [pscustomobject]@{
                        Version = $v
                        Arch    = ($arch -replace '-bit','')
                        Path    = $path
                    }
                }
                catch { }
            }
        }
    }

    if (-not $cands -and (Get-Command python -ErrorAction SilentlyContinue)) {
        try {
            $out = & python -c "import sys,platform; print(f'{sys.version_info[0]}.{sys.version_info[1]}|{platform.architecture()[0]}')" 2>$null
            $ver,$arch = $out.Split("|")
            $v = [version]$ver
            $cands += [pscustomobject]@{
                Version = $v
                Arch    = ($arch -replace '-bit','')
                Path    = (Get-Command python).Source
            }
        }
        catch { }
    }

    $cands | Sort-Object Version -Descending -Unique
}

Write-Host "Searching for installed Python..."
$pyCands = Get-PythonCandidates
if (-not $pyCands -or $pyCands.Count -eq 0) {
    Write-Host "No Python found. Install Python 3.11 and re-run." -ForegroundColor Red
    exit 1
}

$pyCands311 = @($pyCands | Where-Object { $_.Version -eq [version]"3.11" })
if (-not $pyCands311 -or $pyCands311.Count -eq 0) {
    $found = ($pyCands | ForEach-Object { "$($_.Version) $($_.Arch)" } | Sort-Object -Unique) -join ", "
    Write-Host "Python 3.11 is required. Found: $found" -ForegroundColor Red
    Write-Host "Tip (Windows): install Python 3.11 and use: py -3.11" -ForegroundColor Yellow
    exit 1
}

$default = $pyCands311 | Where-Object { $_.Arch -eq "x64" } | Select-Object -First 1
if (-not $default) { $default = $pyCands311 | Select-Object -First 1 }

Write-Host ""
Write-Host "Available Python:"
for ($i=0; $i -lt $pyCands311.Count; $i++) {
    $mark = ""
    if ($pyCands311[$i].Path -eq $default.Path) { $mark = " (default)" }
    Write-Host ("  [{0}] {1} {2} -> {3}{4}" -f $i, $pyCands311[$i].Version, $pyCands311[$i].Arch, $pyCands311[$i].Path, $mark)
}

$chosen = $default
Write-Host ("Using Python {0} {1} -> {2}" -f $chosen.Version, $chosen.Arch, $chosen.Path) -ForegroundColor Green


function Get-VenvPyVersion {
    if (Test-Path ".venv\Scripts\python.exe") {
        & .\.venv\Scripts\python.exe -c "import sys;print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>$null
    }
    else {
        return $null
    }
}

$venvVer = Get-VenvPyVersion
if ($venvVer) {
    if ([version]$venvVer -ne $chosen.Version) {
        Write-Host "Existing .venv is $venvVer; recreating for $($chosen.Version)..."
        try { Remove-Item -Recurse -Force .venv } catch { }
    }
}

if (!(Test-Path ".venv")) {
    Write-Host "Creating .venv..."
    & $chosen.Path -m venv .venv
    if ($LASTEXITCODE -ne 0) { Write-Host "Failed to create venv." -ForegroundColor Red; exit 1 }
}

Write-Host "Installing Python deps (editable)..."

# Best-effort: avoid Windows file-lock issues when pip tries to update console-script wrappers (adaos.exe).
try {
    $running = @()
    $running += @(Get-Process adaos -ErrorAction SilentlyContinue)
    $running += @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        (($_.Name -match '^python(?:w)?\.exe$') -or ($_.Name -match '^py(?:thon)?(?:w)?\.exe$')) -and
        ($_.CommandLine -match '(?:^|[ "\''])-m[ "\'']+adaos(?:[ "\'']|$)')
    })
    if ($running.Count -gt 0) {
        Write-Warning "Found running AdaOS process(es). Close/stop them before reinstalling dependencies to avoid WinError 32 (locked adaos.exe). Prefer running API via .\\.venv\\Scripts\\python.exe -m adaos ..."
    }
}
catch { }

# Best-effort cleanup for interrupted installs (pip can leave '~adaos-*.dist-info' behind).
try {
    $sp = Join-Path $PWD ".venv\\Lib\\site-packages"
    if (Test-Path $sp) {
        Get-ChildItem -Path $sp -Directory -Filter "~adaos-*.dist-info" -ErrorAction SilentlyContinue | ForEach-Object {
            try { Remove-Item -Recurse -Force $_.FullName -ErrorAction SilentlyContinue } catch { }
        }
    }
}
catch { }

.\.venv\Scripts\python.exe -m pip install -U pip
if ($LASTEXITCODE -ne 0) { Write-Host "pip upgrade failed." -ForegroundColor Red; exit 1 }
.\.venv\Scripts\python.exe -m pip install -e .[dev]
if ($LASTEXITCODE -ne 0) { Write-Host "pip install -e . failed." -ForegroundColor Red; exit 1 }

try {
    .\.venv\Scripts\python.exe -c "import adaos; print('adaos import ok')" | Out-Null
}
catch {
    Write-Host "AdaOS is not importable from .venv. Try:" -ForegroundColor Yellow
    Write-Host "  .\\.venv\\Scripts\\python.exe -m pip install -e .[dev]" -ForegroundColor Yellow
    exit 1
}

# .env bootstrap
if (!(Test-Path ".env")) {
    if (Test-Path ".env.sample") {
        Copy-Item ".env.sample" ".env"
        Write-Host ".env created from .env.sample"
    }
    elseif (Test-Path ".env.prod.sample") {
        Copy-Item ".env.prod.sample" ".env"
        Write-Host ".env created from .env.prod.sample"
    }
}

# Default webspace content (scenarios + skills)
# Keep logic inside `adaos install` so presets stay consistent across platforms.
$envType = $env:ENV_TYPE
if ([string]::IsNullOrWhiteSpace($envType)) {
    $env:ENV_TYPE = "dev"
}
$adaosBase = Join-Path $PWD ".adaos"
New-Item -ItemType Directory -Force -Path $adaosBase | Out-Null
$env:ADAOS_BASE_DIR = $adaosBase

Write-Host "Installing default webspace content (adaos install)..."
function Invoke-Adaos {
    param(
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Args
    )
    & .\.venv\Scripts\python.exe -m adaos @Args
}

Invoke-Adaos install
if ($LASTEXITCODE -ne 0) {
    Write-Warning "adaos install failed (check output above)."
}

$env:ADAOS_REV = $Rev

function Test-TcpPortAvailable {
    param(
        [Parameter(Mandatory = $true)][int]$Port
    )
    try {
        # Bind to Any to detect collisions across all local interfaces.
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Any, $Port)
        $listener.Start()
        $listener.Stop()
        return $true
    }
    catch {
        return $false
    }
}

function Ensure-ServePortForJoin {
    if ([string]::IsNullOrWhiteSpace($JoinCode)) { return }

    $servePortExplicit = $PSBoundParameters.ContainsKey("ServePort")
    $controlPortExplicit = $PSBoundParameters.ContainsKey("ControlPort")

    if (-not $servePortExplicit) {
        $cands = @(8778, 8779, 8780, 8781, 8782)
        foreach ($p in $cands) {
            if (Test-TcpPortAvailable -Port $p) { $ServePort = $p; break }
        }
    }
    if (-not $controlPortExplicit) {
        $ControlPort = $ServePort
    }
}

Ensure-ServePortForJoin

function ConvertTo-PowerShellLiteral {
    param(
        [Parameter(Mandatory = $true)][string]$Value
    )
    return "'" + $Value.Replace("'", "''") + "'"
}

function Start-AdaosApiDetached {
    param(
        [Parameter(Mandatory = $true)][string]$BindHost,
        [Parameter(Mandatory = $true)][int]$BindPort
    )
    $pythonExe = (Resolve-Path ".\.venv\Scripts\python.exe").Path
    $repoDir = (Resolve-Path ".").Path
    $powershellExe = (Get-Command powershell).Source
    $command = @"
Set-Location -LiteralPath $(ConvertTo-PowerShellLiteral -Value $repoDir)
`$env:PYTHONUNBUFFERED = '1'
& $(ConvertTo-PowerShellLiteral -Value $pythonExe) -u -m adaos api serve --host $(ConvertTo-PowerShellLiteral -Value $BindHost) --port $BindPort
if (`$LASTEXITCODE -ne 0) {
    Write-Host ('AdaOS API exited with code {0}' -f `$LASTEXITCODE) -ForegroundColor Red
}
"@
    Start-Process `
        -FilePath $powershellExe `
        -WorkingDirectory $repoDir `
        -ArgumentList @("-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NoExit", "-Command", $command) `
        | Out-Null
    Write-Host "AdaOS API started in a separate PowerShell window."
}

function Get-AdaosNodeYamlField {
    param(
        [Parameter(Mandatory = $true)][string]$FieldName
    )
    $nodeYaml = Join-Path $env:ADAOS_BASE_DIR "node.yaml"
    if (!(Test-Path $nodeYaml)) { return $null }
    try {
        $py = ".\\.venv\\Scripts\\python.exe"
        $val = & $py -c "import sys,yaml,pathlib; p=pathlib.Path(sys.argv[1]); d=yaml.safe_load(p.read_text(encoding='utf-8')) or {}; v=d.get(sys.argv[2]); print('' if v is None else v)" $nodeYaml $FieldName 2>$null
        if ([string]::IsNullOrWhiteSpace($val)) { return $null }
        return ($val.Trim())
    }
    catch { return $null }
}

function Wait-AdaosReady {
    param(
        [int]$TimeoutSec = 120
    )
    $token = Get-AdaosNodeYamlField -FieldName "token"
    if (-not $token) { $token = "dev-local-token" }
    $expectedNodeId = Get-AdaosNodeYamlField -FieldName "node_id"
    $base = "http://$ServeHost`:$ControlPort"
    $url = "$base/api/node/status"

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-RestMethod -Method Get -Uri $url -Headers @{ "X-AdaOS-Token" = $token } -TimeoutSec 2
            if ($resp -and $expectedNodeId -and $resp.node_id -and ($resp.node_id -ne $expectedNodeId)) {
                Start-Sleep -Seconds 1
                continue
            }
            if ($resp -and $resp.ready -eq $true) { return $resp }
        }
        catch { }
        Start-Sleep -Seconds 2
    }
    return $null
}

if (-not [string]::IsNullOrWhiteSpace($JoinCode)) {
    Write-Host "Joining subnet via join-code..."
    Invoke-Adaos node join --code $JoinCode --root $RootUrl
    if ($LASTEXITCODE -ne 0) {
        Write-Error "adaos node join failed (see output above)."
        exit 1
    }
}

try {
    $roleNow = Get-AdaosNodeYamlField -FieldName "role"
    $hubNow = Get-AdaosNodeYamlField -FieldName "hub_url"
    $subnetNow = Get-AdaosNodeYamlField -FieldName "subnet_id"
    $nodeNow = Get-AdaosNodeYamlField -FieldName "node_id"
    if ($roleNow -or $hubNow) {
        Write-Host ("Local node.yaml: node_id={0} subnet_id={1} role={2} hub_url={3}" -f $nodeNow, $subnetNow, $roleNow, $hubNow)
    }
}
catch { }

function Resolve-DesiredRole {
    if (-not [string]::IsNullOrWhiteSpace($Role)) { return $Role.Trim().ToLower() }
    if (-not [string]::IsNullOrWhiteSpace($JoinCode)) { return "member" }
    return "hub"
}

$desiredRole = Resolve-DesiredRole
if (-not [string]::IsNullOrWhiteSpace($desiredRole)) {
    if ($desiredRole -notin @("hub", "member")) {
        Write-Warning "Invalid Role '$desiredRole' (expected hub|member). Skipping role set."
    }
    else {
        Write-Host "Setting node role: $desiredRole"
        Invoke-Adaos node role set --role $desiredRole
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "adaos node role set failed (check output above)."
        }
    }
}

Write-Host ("Starting AdaOS API ({0}:{1}) ..." -f $ServeHost, $ServePort)
$serviceInstalled = $false
if ($InstallService -ne "never") {
    try {
        Invoke-Adaos autostart enable --host $ServeHost --port $ServePort | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $serviceInstalled = $true
            Write-Host "Autostart installed (adaos autostart enable)."
        }
    }
    catch {
        Write-Warning "autostart enable failed: $($_.Exception.Message)"
    }
}

if ($serviceInstalled) {
    try { schtasks /Run /TN "AdaOS" | Out-Null } catch { }
}

if (-not $serviceInstalled -or $InstallService -eq "never") {
    try {
        Start-AdaosApiDetached -BindHost $ServeHost -BindPort $ServePort
        Write-Host "AdaOS API started as a detached process."
    }
    catch {
        Write-Warning "Failed to start adaos api serve as a detached process. Run in foreground:"
        Write-Host ("  .\\.venv\\Scripts\\python.exe -u -m adaos api serve --host {0} --port {1}" -f $ServeHost, $ServePort)
    }
}

$st = Wait-AdaosReady -TimeoutSec 120
if ($st) {
    Write-Host ("READY: node_id={0} subnet_id={1} role={2} route={3} connected={4}" -f $st.node_id, $st.subnet_id, $st.role, $st.route_mode, $st.connected_to_hub) -ForegroundColor Green
}
else {
    Write-Warning "Node did not become ready in time. Check logs or run:"
    Write-Host ("  .\\.venv\\Scripts\\python.exe -m adaos node status --control http://{0}:{1}" -f $ServeHost, $ControlPort)
}

$helpText = @'
READY.

Next steps (in separate terminals):
  1) Activate venv:
     .\.venv\Scripts\Activate.ps1
  2) CLI:
     .\.venv\Scripts\python.exe -m adaos --help
  3) API:
     .\.venv\Scripts\python.exe -m adaos api serve --host 127.0.0.1 --port 8777 --reload
  4) Backend (Inimatic):
     cd src\adaos\integrations\inimatic
     npm i
     npm run start:api-dev
  5) Frontend (Inimatic):
     cd src\adaos\integrations\inimatic
     npm run start

Tips:
 - List installed Python: py -0p
 - Switch venv version: delete .venv and re-run bootstrap
'@
Write-Host $helpText
