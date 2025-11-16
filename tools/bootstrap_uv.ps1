# tools/bootstrap_uv.ps1
$ErrorActionPreference = "Stop"
$SUBMODULE_PATH = "src/adaos/integrations/inimatic"

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

# 3) Frontend deps (optional)
Push-Location $SUBMODULE_PATH
try {
  if (Have "pnpm") {
    pnpm install
    if ($LASTEXITCODE -ne 0) { throw "pnpm install failed" }
    $USED_PKG_CMD = "pnpm install"
  } else {
    # сначала пытаемся reproducible ci, если lock синхронен
    npm ci --no-audit --no-fund
    if ($LASTEXITCODE -eq 0) {
      $USED_PKG_CMD = "npm ci"
    } else {
      Write-Warning "npm ci failed; updating lock with npm install..."
      npm install --no-audit --no-fund
      if ($LASTEXITCODE -ne 0) { throw "npm install failed" }
      $USED_PKG_CMD = "npm install"
    }
  }
  Write-Host "Frontend dependencies installed ($USED_PKG_CMD)."
}
finally {
  Pop-Location
}

# 4) .env bootstrap
if (-not (Test-Path ".env") -and (Test-Path ".env.example")) {
  Copy-Item ".env.example" ".env"
  Write-Host ".env created from .env.example"
}

# 5) Short command: add .venv\Scripts to PATH for current session
$venvBin = Join-Path $PWD ".venv\Scripts"
if (Test-Path $venvBin) {
  $env:Path = "$venvBin;$env:Path"
}

# 6) Install default webspace content (scenarios/skills)
$defaultScenarios = @("web_desktop")
$defaultSkills = @("weather_skill")
$adaosBase = Join-Path $PWD ".adaos"
New-Item -ItemType Directory -Force -Path $adaosBase | Out-Null
$env:ADAOS_BASE_DIR = $adaosBase

foreach ($scenario in $defaultScenarios) {
  Write-Host "Installing scenario $scenario..."
  uv run adaos scenario install $scenario | Out-Host
  if ($LASTEXITCODE -ne 0) {
    Write-Warning "Scenario '$scenario' install failed (possibly already installed)."
  }
}
foreach ($skill in $defaultSkills) {
  Write-Host "Installing skill $skill..."
  uv run adaos skill install $skill | Out-Host
  if ($LASTEXITCODE -ne 0) {
    Write-Warning "Skill '$skill' install failed (possibly already installed)."
  }
}

Write-Host ""
Write-Host "Bootstrap completed."
Write-Host "Quick checks:"
Write-Host "  uv --version"
Write-Host "  uv run python -V"
Write-Host "  adaos --help     (short command should work in this session)"
Write-Host ""
Write-Host "To run the API:"
Write-Host "  uv run adaos api serve --host 127.0.0.1 --port 8777 --reload"
