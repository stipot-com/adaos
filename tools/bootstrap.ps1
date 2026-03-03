# tools/bootstrap.ps1
# Unified bootstrap for Windows PowerShell 5.1+

param(
    [string]$JoinCode = "",
    [string]$Role = "member",
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

$choice = Read-Host "Pick index for .venv (Enter = default)"
if ([string]::IsNullOrWhiteSpace($choice)) {
    $chosen = $default
}
elseif ($choice -notmatch '^\d+$' -or [int]$choice -ge $pyCands311.Count) {
    Write-Host "Invalid choice. Using default." -ForegroundColor Yellow
    $chosen = $default
}
else {
    $chosen = $pyCands311[[int]$choice]
}
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
.\.venv\Scripts\python.exe -m pip install -U pip
if ($LASTEXITCODE -ne 0) { Write-Host "pip upgrade failed." -ForegroundColor Red; exit 1 }
.\.venv\Scripts\python.exe -m pip install -e .[dev]
if ($LASTEXITCODE -ne 0) { Write-Host "pip install -e . failed." -ForegroundColor Red; exit 1 }

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
& .\.venv\Scripts\adaos.exe install
if ($LASTEXITCODE -ne 0) {
    Write-Warning "adaos install failed (check output above)."
}

$env:ADAOS_REV = $Rev

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
    $base = "http://$ServeHost`:$ControlPort"
    $url = "$base/api/node/status"

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-RestMethod -Method Get -Uri $url -Headers @{ "X-AdaOS-Token" = $token } -TimeoutSec 2
            if ($resp -and $resp.ready -eq $true) { return $resp }
        }
        catch { }
        Start-Sleep -Seconds 2
    }
    return $null
}

if (-not [string]::IsNullOrWhiteSpace($JoinCode)) {
    Write-Host "Joining subnet via join-code..."
    & .\.venv\Scripts\adaos.exe node join --code $JoinCode --root $RootUrl
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "adaos node join failed (check output above)."
    }
}

if (-not [string]::IsNullOrWhiteSpace($Role)) {
    Write-Host "Setting node role: $Role"
    & .\.venv\Scripts\adaos.exe node role set --role $Role
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "adaos node role set failed (check output above)."
    }
}

Write-Host "Starting AdaOS API ($ServeHost:$ServePort) ..."
$serviceInstalled = $false
if ($InstallService -ne "never") {
    try {
        & .\.venv\Scripts\adaos.exe autostart enable --host $ServeHost --port $ServePort | Out-Null
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
        Start-Process -FilePath ".\\.venv\\Scripts\\adaos.exe" -ArgumentList @("api", "serve", "--host", $ServeHost, "--port", "$ServePort") -WindowStyle Hidden | Out-Null
    }
    catch {
        Write-Warning "Failed to start adaos api serve in background. Run in foreground:"
        Write-Host ("  .\\.venv\\Scripts\\adaos.exe api serve --host {0} --port {1}" -f $ServeHost, $ServePort)
    }
}

$st = Wait-AdaosReady -TimeoutSec 120
if ($st) {
    Write-Host ("READY: node_id={0} subnet_id={1} role={2} route={3} connected={4}" -f $st.node_id, $st.subnet_id, $st.role, $st.route_mode, $st.connected_to_hub) -ForegroundColor Green
}
else {
    Write-Warning "Node did not become ready in time. Check logs or run:"
    Write-Host ("  .\\.venv\\Scripts\\adaos.exe node status --control http://{0}:{1}" -f $ServeHost, $ControlPort)
}

$helpText = @'
READY.

Next steps (in separate terminals):
  1) Activate venv:
     .\.venv\Scripts\Activate.ps1
  2) CLI:
     adaos --help
  3) API:
     adaos api serve --host 127.0.0.1 --port 8777 --reload
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
