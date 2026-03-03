# tools/bootstrap_uv.ps1
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
$SUBMODULE_PATH = "src/adaos/integrations/inimatic"

# Ensure we operate from repo root even if invoked from elsewhere.
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

function Have($cmd) {
  try { Get-Command $cmd -ErrorAction Stop | Out-Null; return $true }
  catch { return $false }
}

# 1) Install uv if missing
if (-not (Have "uv")) {
  Write-Host "Installing uv..."
  Invoke-WebRequest -UseBasicParsing https://astral.sh/uv/install.ps1 | Invoke-Expression
  $env:Path = "$HOME\.local\bin;$env:Path"
}

# Python 3.11 only (uv-managed)
Write-Host "Ensuring Python 3.11..."
uv python install 3.11
if ($LASTEXITCODE -ne 0) { throw "uv python install 3.11 failed" }
$env:UV_PYTHON = "3.11"

# 2) Sync Python deps (creates .venv and installs project)
if (Test-Path "uv.lock") {
  Write-Host "Syncing environment from uv.lock..."
  uv sync --locked
  if ($LASTEXITCODE -ne 0) {
    Write-Warning "uv sync --locked failed, refreshing lock..."
    uv lock
    if ($LASTEXITCODE -ne 0) { throw "uv lock failed" }
    uv sync
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }
  }
} else {
  Write-Host "Locking and syncing environment..."
  uv lock
  if ($LASTEXITCODE -ne 0) { throw "uv lock failed" }
  uv sync
  if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }
}

# 4) .env bootstrap
if (-not (Test-Path ".env")) {
  if (Test-Path ".env.sample") {
    Copy-Item ".env.sample" ".env"
    Write-Host ".env created from .env.sample"
  }
  elseif (Test-Path ".env.prod.sample") {
    Copy-Item ".env.prod.sample" ".env"
    Write-Host ".env created from .env.prod.sample"
  }
}

# 5) Short command: add .venv\Scripts to PATH for current session
$venvBin = Join-Path $PWD ".venv\Scripts"
if (Test-Path $venvBin) {
  $env:Path = "$venvBin;$env:Path"
}

# Prefer invoking AdaOS via `python -m adaos` to avoid console-script wrapper issues.
function Invoke-Adaos {
  param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
  )
  & .\.venv\Scripts\python.exe -m adaos @Args
}

# 6) Default webspace content (scenarios + skills) via built-in `adaos install`
$envType = $env:ENV_TYPE
if ([string]::IsNullOrWhiteSpace($envType)) {
  $env:ENV_TYPE = "dev"
}
$adaosBase = Join-Path $PWD ".adaos"
New-Item -ItemType Directory -Force -Path $adaosBase | Out-Null
$env:ADAOS_BASE_DIR = $adaosBase
Write-Host "Installing default webspace content (adaos install)..."
Invoke-Adaos install
if ($LASTEXITCODE -ne 0) {
  Write-Warning "adaos install failed (check output above)."
}

$env:ADAOS_REV = $Rev

function Get-AdaosNodeYamlField {
  param([Parameter(Mandatory = $true)][string]$FieldName)
  $nodeYaml = Join-Path $env:ADAOS_BASE_DIR "node.yaml"
  if (!(Test-Path $nodeYaml)) { return $null }
  try {
    $py = ".\\.venv\\Scripts\\python.exe"
    $val = & $py -c "import sys,yaml,pathlib; p=pathlib.Path(sys.argv[1]); d=yaml.safe_load(p.read_text(encoding='utf-8')) or {}; v=d.get(sys.argv[2]); print('' if v is None else v)" $nodeYaml $FieldName 2>$null
    if ([string]::IsNullOrWhiteSpace($val)) { return $null }
    return ($val.Trim())
  } catch { return $null }
}

function Wait-AdaosReady {
  param([int]$TimeoutSec = 120)
  $token = Get-AdaosNodeYamlField -FieldName "token"
  if (-not $token) { $token = "dev-local-token" }
  $base = "http://$ServeHost`:$ControlPort"
  $url = "$base/api/node/status"
  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  while ((Get-Date) -lt $deadline) {
    try {
      $resp = Invoke-RestMethod -Method Get -Uri $url -Headers @{ "X-AdaOS-Token" = $token } -TimeoutSec 2
      if ($resp -and $resp.ready -eq $true) { return $resp }
    } catch { }
    Start-Sleep -Seconds 2
  }
  return $null
}

if (-not [string]::IsNullOrWhiteSpace($JoinCode)) {
  Write-Host "Joining subnet via join-code..."
  Invoke-Adaos node join --code $JoinCode --root $RootUrl | Out-Null
  if ($LASTEXITCODE -ne 0) {
    Write-Error "adaos node join failed (see output above)."
    exit 1
  }
}

if (-not [string]::IsNullOrWhiteSpace($Role)) {
  $roleNorm = $Role.Trim().ToLower()
  if ($roleNorm -notin @("hub", "member")) {
    Write-Warning "Invalid Role '$Role' (expected hub|member). Skipping role set."
  }
  else {
    Write-Host "Setting node role: $roleNorm"
    Invoke-Adaos node role set --role $roleNorm | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Warning "adaos node role set failed (check output above)." }
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
  } catch { Write-Warning "autostart enable failed: $($_.Exception.Message)" }
}
if ($serviceInstalled) {
  try { schtasks /Run /TN "AdaOS" | Out-Null } catch { }
}
if (-not $serviceInstalled -or $InstallService -eq "never") {
  try {
    Start-Process -FilePath ".\\.venv\\Scripts\\python.exe" -ArgumentList @("-m", "adaos", "api", "serve", "--host", $ServeHost, "--port", "$ServePort") -WindowStyle Hidden | Out-Null
  } catch {
    Write-Warning "Failed to start adaos api serve. Run:"
    Write-Host ("  .\\.venv\\Scripts\\python.exe -m adaos api serve --host {0} --port {1}" -f $ServeHost, $ServePort)
  }
}

$st = Wait-AdaosReady -TimeoutSec 120
if ($st) {
  Write-Host ("READY: node_id={0} subnet_id={1} role={2} route={3} connected={4}" -f $st.node_id, $st.subnet_id, $st.role, $st.route_mode, $st.connected_to_hub) -ForegroundColor Green
} else {
  Write-Warning "Node did not become ready in time. Check:"
  Write-Host ("  .\\.venv\\Scripts\\python.exe -m adaos node status --control http://{0}:{1}" -f $ServeHost, $ControlPort)
}

Write-Host ""
Write-Host "Bootstrap completed."
Write-Host "Quick checks:"
Write-Host "  uv --version"
Write-Host "  uv run python -V"
Write-Host "  adaos --help     (short command should work in this session)"
Write-Host ""
Write-Host "Activate virtual environment"
Write-Host " ./.venv/Scripts/Activate.ps1"
Write-Host ""
Write-Host "Re-install base scenarios/skills (idempotent):"
Write-Host "  adaos install"
Write-Host "To run the API:"
Write-Host "  adaos api serve"
