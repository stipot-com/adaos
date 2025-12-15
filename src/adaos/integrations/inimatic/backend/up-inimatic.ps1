$EnvFile = Join-Path $Backend "deployment\.env"
if (Test-Path $EnvFile) {
  Get-Content $EnvFile | ForEach-Object {
    if ($_ -match '^\s*#') { return }
    if ($_ -match '^\s*$') { return }
    $kv = $_ -split '=', 2
    if ($kv.Length -eq 2) {
      $name = $kv[0].Trim()
      $value = $kv[1].Trim()
      [System.Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
  }
  Write-Host "[info] Loaded env from $EnvFile"
} else {
  Write-Host "[warn] Env file not found: $EnvFile"
}

param(
  [switch]$WithMcp
)

$ErrorActionPreference = "Stop"

# paths
$Repo = (Resolve-Path "$PSScriptRoot\..").Path
$Backend = Join-Path $Repo "C:\Users\Danil\Documents\GitHub\MCP\src\adaos\integrations\inimatic\backend"
$Mcp = Join-Path $Repo "C:\Users\Danil\Documents\GitHub\MCP\src\adaos\integrations\inimatic\mcp"

Write-Host "[1/5] Backend deps..."
Push-Location $Backend
npm install | Out-Host

# Optional: check env presence (minimal)
if (-not $env:CA_KEY_PEM -and -not $env:CA_KEY_PEM_FILE) {
  Write-Host "[warn] CA_KEY_PEM / CA_KEY_PEM_FILE not set in this shell. Backend may fail if it can't read certs."
}
if (-not $env:CA_CERT_PEM -and -not $env:CA_CERT_PEM_FILE) {
  Write-Host "[warn] CA_CERT_PEM / CA_CERT_PEM_FILE not set in this shell. Backend may fail if it can't read certs."
}

Write-Host "[2/5] Start backend..."
$backendJob = Start-Process -PassThru -WindowStyle Normal powershell `
  -ArgumentList "-NoExit", "-Command", "cd `"$Backend`"; npm run dev"

Pop-Location

Start-Sleep -Seconds 1

Write-Host "[3/5] Wait health..."
$ok = $false
for ($i=0; $i -lt 30; $i++) {
  try {
    $out = & curl.exe -sk https://localhost:3030/v1/health
    if ($out -match '"ok"\s*:\s*true') { $ok = $true; break }
  } catch {}
  Start-Sleep -Milliseconds 500
}

if (-not $ok) {
  Write-Host "[error] Backend did not become healthy on https://localhost:3030/v1/health"
  Write-Host "        Check backend console window logs."
  exit 1
}

Write-Host "[ok] Backend is healthy."

if ($WithMcp) {
  Write-Host "[4/5] MCP deps..."
  Push-Location $Mcp
  npm install | Out-Host

  Write-Host "[5/5] Start MCP tools-test..."
  Start-Process -WindowStyle Normal powershell `
    -ArgumentList "-NoExit", "-Command", "cd `"$Mcp`"; node .\tools-test.mjs"

  Pop-Location
}

Write-Host "[done] Started. Backend PID=$($backendJob.Id)"
