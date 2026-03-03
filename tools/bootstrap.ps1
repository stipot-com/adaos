# tools/bootstrap.ps1
# Unified bootstrap for Windows PowerShell 5.1+

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
