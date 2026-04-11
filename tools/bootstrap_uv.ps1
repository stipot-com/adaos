# tools/bootstrap_uv.ps1
param(
  [string]$JoinCode = "",
  [string]$Role = "",
  [switch]$Dev,
  [switch]$NoVoice,
  [ValidateSet("auto", "always", "never")]
  [string]$InstallService = "auto",
  [string]$ServeHost = "127.0.0.1",
  [int]$ServePort = 8777,
  [int]$ControlPort = 8777,
  [string]$RootUrl = "https://api.inimatic.com",
  [string]$Rev = "rev2026",
  [string]$ZoneId = ""
)

$ErrorActionPreference = "Stop"
$clientSubPath = "src\adaos\integrations\adaos-client"
$backendSubPath = "src\adaos\integrations\adaos-backend"
$infraSubPath = "src\adaos\integrations\infra-inimatic"

# Ensure we operate from repo root even if invoked from elsewhere.
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot
if (-not [string]::IsNullOrWhiteSpace($ZoneId)) {
  $ZoneId = $ZoneId.Trim().ToLower()
  if ($ZoneId -notmatch '^[a-z]{2}$') {
    throw "ZoneId must be a two-letter lowercase country/region code (example: ru)"
  }
}

function Have($cmd) {
  try { Get-Command $cmd -ErrorAction Stop | Out-Null; return $true }
  catch { return $false }
}

function Write-EnvVar {
  param(
    [Parameter(Mandatory = $true)][string]$Key,
    [Parameter(Mandatory = $true)][string]$Value,
    [string]$EnvFile = ".env"
  )
  if ([string]::IsNullOrWhiteSpace($Key)) { return }
  if (-not (Test-Path $EnvFile)) {
    New-Item -ItemType File -Path $EnvFile -Force | Out-Null
  }
  $lines = @()
  if (Test-Path $EnvFile) {
    $lines = @(Get-Content $EnvFile -ErrorAction SilentlyContinue)
  }
  $updated = $false
  for ($i = 0; $i -lt $lines.Count; $i++) {
    if ($lines[$i] -match "^\Q$Key\E=") {
      $lines[$i] = "$Key=$Value"
      $updated = $true
      break
    }
  }
  if (-not $updated) {
    $lines += "$Key=$Value"
  }
  Set-Content -Path $EnvFile -Value $lines
}

function Resolve-EffectiveRootUrl {
  param(
    [string]$RootUrlValue,
    [string]$ZoneValue
  )
  $normalizedZone = [string]$ZoneValue
  if ([string]::IsNullOrWhiteSpace($normalizedZone)) { $normalizedZone = "" }
  $normalizedZone = $normalizedZone.Trim().ToLower()
  if ($normalizedZone -notmatch '^[a-z]{2}$') { $normalizedZone = "" }
  $normalizedRoot = [string]$RootUrlValue
  if ([string]::IsNullOrWhiteSpace($normalizedRoot)) { $normalizedRoot = "https://api.inimatic.com" }
  $normalizedRoot = $normalizedRoot.Trim().TrimEnd("/")
  if ($normalizedZone -eq "ru" -and $normalizedRoot -in @("https://api.inimatic.com", "http://api.inimatic.com")) {
    return "https://$normalizedZone.api.inimatic.com"
  }
  return $normalizedRoot
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
  if (Test-Path ".env.example") {
    Copy-Item ".env.example" ".env"
    Write-Host ".env created from .env.example"
  }
  elseif (Test-Path ".env.prod.sample") {
    Copy-Item ".env.prod.sample" ".env"
    Write-Host ".env created from .env.prod.sample"
  }
}
if (-not [string]::IsNullOrWhiteSpace($ZoneId)) {
  Write-EnvVar -Key "ADAOS_ZONE_ID" -Value $ZoneId.Trim().ToLower() -EnvFile ".env"
}
if ($Dev) {
  Write-EnvVar -Key "ENV_TYPE" -Value "dev" -EnvFile ".env"
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

function Install-VoiceDeps {
  if ($NoVoice) { return }
  Write-Host "Installing voice deps (Rasa)..."
  $pyVer = ""
  try {
    $pyVer = (& .\.venv\Scripts\python.exe -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>$null).Trim()
  } catch { }
  if ($pyVer -in @("3.11", "3.12", "3.13")) {
    Write-Warning "Skipping voice NLU deps: rasa==3.6.21 is not available for Python $pyVer."
    Write-Warning "If you need voice NLU, use Python 3.10 or run with -NoVoice to silence this step."
    return
  }
  try {
    & .\.venv\Scripts\python.exe -c "import rasa; print(getattr(rasa,'__version__',''))" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
      Write-Host "Rasa already installed."
      return
    }
  } catch { }
  try {
    & .\.venv\Scripts\python.exe -m pip install "rasa==3.6.21"
    if ($LASTEXITCODE -ne 0) { throw "pip install rasa failed" }
    Write-Host "Rasa installed."
  } catch {
    Write-Warning "Rasa install failed. Continue without voice NLU (use -NoVoice to skip)."
  }
}

# 6) Default webspace content (scenarios + skills) via built-in `adaos install`
$envType = $env:ENV_TYPE
if ([string]::IsNullOrWhiteSpace($envType) -and (Test-Path ".env")) {
  try {
    $m = Select-String -Path ".env" -Pattern "^\s*ENV_TYPE\s*=" -SimpleMatch:$false | Select-Object -First 1
    if ($m -and $m.Line) {
      $v = ($m.Line -split "=", 2)[1].Trim().Trim("'").Trim('"')
      if (-not [string]::IsNullOrWhiteSpace($v)) { $envType = $v }
    }
  } catch { }
}
if ([string]::IsNullOrWhiteSpace($envType)) { $envType = "dev" }
if ($Dev) { $envType = "dev" }
$env:ENV_TYPE = $envType

if ([string]::IsNullOrWhiteSpace($env:ADAOS_BASE_DIR)) {
  if ($envType -eq "dev") {
    $env:ADAOS_BASE_DIR = (Join-Path $PWD ".adaos")
  } else {
    $env:ADAOS_BASE_DIR = (Join-Path $HOME ".adaos")
  }
}
New-Item -ItemType Directory -Force -Path $env:ADAOS_BASE_DIR | Out-Null
Write-Host "Detecting git availability (adaos git autodetect)..."
try { Invoke-Adaos git autodetect | Out-Null } catch { }
Write-Host "Installing default webspace content (adaos install)..."
Invoke-Adaos install
if ($LASTEXITCODE -ne 0) {
  Write-Warning "adaos install failed (check output above)."
}

Install-VoiceDeps

try {
  .\.venv\Scripts\python.exe -c "import adaos; print('adaos import ok')" | Out-Null
} catch {
  Write-Error "AdaOS is not importable from .venv. Try: uv sync (or delete .venv and re-run bootstrap)."
  exit 1
}

$effectiveRootUrl = Resolve-EffectiveRootUrl -RootUrlValue $RootUrl -ZoneValue $ZoneId
$env:ADAOS_REV = $Rev
$env:ADAOS_API_BASE = $effectiveRootUrl
if (-not [string]::IsNullOrWhiteSpace($ZoneId)) {
  $env:ADAOS_ZONE_ID = $ZoneId.Trim().ToLower()
}

function Test-TcpPortAvailable {
  param([Parameter(Mandatory = $true)][int]$Port)
  try {
    # Bind to Any to detect collisions across all local interfaces.
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Any, $Port)
    $listener.Start()
    $listener.Stop()
    return $true
  } catch { return $false }
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
  if (-not $controlPortExplicit) { $ControlPort = $ServePort }
}

Ensure-ServePortForJoin

function ConvertTo-PowerShellLiteral {
  param([Parameter(Mandatory = $true)][string]$Value)
  return "'" + $Value.Replace("'", "''") + "'"
}

function Start-AdaosApiDetached {
  param(
    [Parameter(Mandatory = $true)][string]$BindHost,
    [Parameter(Mandatory = $true)][int]$BindPort
  )
  $pythonExe = (Resolve-Path ".\\.venv\\Scripts\\python.exe").Path
  $repoDir = (Resolve-Path ".").Path
  Start-Process `
    -FilePath $pythonExe `
    -WorkingDirectory $repoDir `
    -WindowStyle Hidden `
    -ArgumentList @("-u", "-m", "adaos", "api", "serve", "--host", $BindHost, "--port", "$BindPort") `
    | Out-Null
}

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
    } catch { }
    Start-Sleep -Seconds 2
  }
  return $null
}

function Show-QrIfAvailable {
  param([Parameter(Mandatory = $true)][string]$Text)
  if ([string]::IsNullOrWhiteSpace($Text)) { return }
  try {
    $cmd = Get-Command qrencode -ErrorAction SilentlyContinue
    if (-not $cmd) { return }
    Write-Host ""
    Write-Host "     (QR)"
    & qrencode -t ANSIUTF8 $Text 2>$null
    Write-Host ""
  } catch { }
}

function Show-OptionalModulesNote {
  $missing = New-Object System.Collections.Generic.List[string]
  if (-not (Test-Path (Join-Path $clientSubPath "package.json"))) { $missing.Add($clientSubPath) }
  if (-not (Test-Path (Join-Path $backendSubPath "package.json"))) { $missing.Add($backendSubPath) }
  if (-not (Test-Path (Join-Path $infraSubPath "README.md"))) { $missing.Add($infraSubPath) }

  Write-Host ""
  Write-Host "Optional private modules:"
  Write-Host ("  Client:  {0}" -f $clientSubPath)
  Write-Host ("  Backend: {0}" -f $backendSubPath)
  Write-Host ("  Infra:   {0}" -f $infraSubPath)
  if ($missing.Count -gt 0) {
    Write-Host "  Missing locally. Initialize only if you need them:"
    Write-Host ("    git submodule update --init --recursive {0}" -f ($missing -join " "))
  }
}

if (-not [string]::IsNullOrWhiteSpace($JoinCode)) {
  Write-Host "Joining subnet via join-code..."
  Invoke-Adaos node join --code $JoinCode --root $effectiveRootUrl | Out-Null
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
} catch { }

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
    Invoke-Adaos node role set --role $desiredRole | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Warning "adaos node role set failed (check output above)." }
  }
}

if ($desiredRole -eq "hub") {
  try {
    Write-Host "Initializing Root subnet (adaos dev root init)..."
    Invoke-Adaos dev root init | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Warning "adaos dev root init failed (check output above)." }
  } catch { Write-Warning "adaos dev root init failed: $($_.Exception.Message)" }
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
  try {
    $as = Invoke-Adaos autostart status --json 2>$null | Out-String
    if (-not [string]::IsNullOrWhiteSpace($as)) {
      $asObj = $as | ConvertFrom-Json
      $active = $asObj.active
      $listening = $asObj.listening
      if (($active -ne $true) -or ($listening -eq $false)) {
        Write-Warning "Autostart is enabled but not active; falling back to detached process."
        $serviceInstalled = $false
      }
    }
  } catch { }
}
if (-not $serviceInstalled -or $InstallService -eq "never") {
  try {
    Start-AdaosApiDetached -BindHost $ServeHost -BindPort $ServePort
    Write-Host "AdaOS API started as a detached process."
  } catch {
    Write-Warning "Failed to start adaos api serve as a detached process. Run:"
    Write-Host ("  .\\.venv\\Scripts\\python.exe -u -m adaos api serve --host {0} --port {1}" -f $ServeHost, $ServePort)
  }
}

$st = Wait-AdaosReady -TimeoutSec 120
$deepLink = $null
$tgPairCode = $null
$ownerAuth = $null
try {
  Write-Host "Generating Telegram pairing link..."
  $tg = Invoke-Adaos dev telegram 2>$null | Out-String
  if (-not [string]::IsNullOrWhiteSpace($tg)) {
    $codeLine = ($tg -split "`r?`n") | Where-Object { $_ -match "^\s*pair_code:\s*" } | Select-Object -First 1
    if ($codeLine) { $tgPairCode = ($codeLine -replace "^\s*pair_code:\s*", "").Trim() }
    $deepLine = ($tg -split "`r?`n") | Where-Object { $_ -match "^\s*deep_link:\s*" } | Select-Object -First 1
    if ($deepLine) { $deepLink = ($deepLine -replace "^\s*deep_link:\s*", "").Trim() }
  }
} catch { }

try {
  Write-Host "Generating Owner browser pairing code..."
  $ownerJson = Invoke-Adaos dev root login --print-only --json 2>$null | Out-String
  if (-not [string]::IsNullOrWhiteSpace($ownerJson)) {
    $ownerAuth = $ownerJson | ConvertFrom-Json
  }
} catch { }

if ($st) {
  Write-Host ("READY: node_id={0} subnet_id={1} role={2} route={3} connected={4}" -f $st.node_id, $st.subnet_id, $st.role, $st.route_mode, $st.connected_to_hub) -ForegroundColor Green
} else {
  Write-Warning "Node did not become ready in time. Check:"
  Write-Host ("  .\\.venv\\Scripts\\python.exe -m adaos node status --control http://{0}:{1}" -f $ServeHost, $ControlPort)
}

Write-Host ""
Write-Host "Bootstrap completed."
Write-Host ""
Write-Host "Next steps:"
if ($deepLink) {
  Write-Host "  1) Telegram: open and confirm pairing:"
  Write-Host ("     {0}" -f $deepLink)
  if ($tgPairCode) { Write-Host ("     pair_code: {0}" -f $tgPairCode) }
  Show-QrIfAvailable -Text $deepLink
} else {
  Write-Host "  1) Telegram pairing:"
  Write-Host "     .\\.venv\\Scripts\\python.exe -m adaos dev telegram"
}
Write-Host "  2) Owner browser:"
if ($ownerAuth -and $ownerAuth.verification_uri_complete) {
  Write-Host ("     Open: {0}" -f $ownerAuth.verification_uri_complete)
  Write-Host ("     user_code: {0}" -f $ownerAuth.user_code)
  Show-QrIfAvailable -Text $ownerAuth.verification_uri_complete
} elseif ($ownerAuth -and $ownerAuth.verification_uri) {
  Write-Host ("     Open: {0}" -f $ownerAuth.verification_uri)
  Write-Host ("     user_code: {0}" -f $ownerAuth.user_code)
  Show-QrIfAvailable -Text $ownerAuth.verification_uri
} else {
  Write-Host "     .\\.venv\\Scripts\\python.exe -m adaos dev root login"
  Write-Host "     Then open https://app.inimatic.com/?mode=registration and enter the code."
}
Write-Host "  3) Start/stop/restart AdaOS API:"
Write-Host ("     Start (foreground): .\\.venv\\Scripts\\python.exe -m adaos api serve --host {0} --port {1}" -f $ServeHost, $ServePort)
Write-Host "     Stop:              .\\.venv\\Scripts\\python.exe -m adaos api stop"
Write-Host "     Restart:           .\\.venv\\Scripts\\python.exe -m adaos api restart"
Write-Host "  4) Web UI:"
Write-Host "     Open https://myinimatic.web.app/ and connect to your local node (ports 8777/8778)."
if ($st -and $st.role -eq "member") {
  Write-Host "  5) Member → hub connectivity:"
  Write-Host ("     connected_to_hub={0}" -f $st.connected_to_hub)
  Write-Host "     Details: .\\.venv\\Scripts\\python.exe -m adaos node status"
}
Write-Host ""
Write-Host "Docs:"
Write-Host "  https://stipot-com.github.io/adaos/"
Show-OptionalModulesNote
if (-not (Get-Command qrencode -ErrorAction SilentlyContinue)) {
  Write-Host ""
  Write-Host "Tip: install 'qrencode' to show QR codes in terminal."
}
