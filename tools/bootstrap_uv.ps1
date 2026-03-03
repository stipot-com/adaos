# tools/bootstrap_uv.ps1
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

# 6) Default webspace content (scenarios + skills) via built-in `adaos install`
$envType = $env:ENV_TYPE
if ([string]::IsNullOrWhiteSpace($envType)) {
  $env:ENV_TYPE = "dev"
}
$adaosBase = Join-Path $PWD ".adaos"
New-Item -ItemType Directory -Force -Path $adaosBase | Out-Null
$env:ADAOS_BASE_DIR = $adaosBase
Write-Host "Installing default webspace content (adaos install)..."
uv run adaos install
if ($LASTEXITCODE -ne 0) {
  Write-Warning "adaos install failed (check output above)."
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
