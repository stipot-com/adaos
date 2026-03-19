# Minimal "download & bootstrap" entrypoint (Windows PowerShell 5.1+).
# Served as a static asset:
#   https://app.inimatic.com/assets/windows/init.ps1

[CmdletBinding(PositionalBinding = $false)]
param(
  [string]$Dest = "$HOME\\adaos",
  [string]$Rev = "rev2026",
  [string]$RepoOwner = "stipot-com",
  [string]$RepoName = "adaos",
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$BootstrapArgs
)

$ErrorActionPreference = "Stop"

function Write-Info([string]$s) { Write-Host "[*] $s" -ForegroundColor Cyan }
function Write-Ok([string]$s) { Write-Host "[+] $s" -ForegroundColor Green }
function Write-Warn([string]$s) { Write-Host "[!] $s" -ForegroundColor Yellow }
function Have([string]$cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

try {
  # Ensure modern TLS for GitHub downloads.
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
} catch { }

if ([string]::IsNullOrWhiteSpace($Dest)) { throw "Dest is empty" }
if ([string]::IsNullOrWhiteSpace($Rev)) { throw "Rev is empty" }

Write-Info ("Preparing repo at: {0}" -f $Dest)
New-Item -ItemType Directory -Force -Path $Dest | Out-Null

if (-not (Have "git")) {
  Write-Info "git not found; trying to install (best-effort)..."
  try {
    if (Have "winget") {
      winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements | Out-Null
    }
    elseif (Have "choco") {
      choco install git -y | Out-Null
    }
  } catch { }
  if (Have "git") {
    Write-Ok "git installed"
  } else {
    Write-Warn "git is not available; AdaOS will run in archive (no-git) mode for skills/scenarios until you enable git"
  }
}

$zipUrl = "https://github.com/$RepoOwner/$RepoName/archive/refs/heads/$Rev.zip"
$tmp = Join-Path $env:TEMP ("adaos_init_{0}" -f [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$zipPath = Join-Path $tmp "adaos.zip"

try {
  Write-Info ("Downloading source archive: {0}" -f $zipUrl)
  Invoke-WebRequest -UseBasicParsing -Uri $zipUrl -OutFile $zipPath
  Write-Info "Extracting..."
  Expand-Archive -LiteralPath $zipPath -DestinationPath $tmp -Force

  $extracted = Get-ChildItem -LiteralPath $tmp -Directory | Where-Object { $_.Name -like "$RepoName-*" } | Select-Object -First 1
  if (-not $extracted) { throw "Failed to locate extracted directory in $tmp" }

  if (Test-Path $Dest) {
    try { Remove-Item -Recurse -Force -LiteralPath $Dest } catch { }
  }
  New-Item -ItemType Directory -Force -Path $Dest | Out-Null
  Copy-Item -Recurse -Force -LiteralPath (Join-Path $extracted.FullName "*") -Destination $Dest
  Write-Ok ("Source extracted to: {0}" -f $Dest)
}
finally {
  try { Remove-Item -Recurse -Force -LiteralPath $tmp } catch { }
}

Set-Location -LiteralPath $Dest

# Ensure bootstrap gets -Rev unless caller already passed -Rev/--rev.
$haveRev = $false
for ($i = 0; $i -lt $BootstrapArgs.Count; $i++) {
  if ($BootstrapArgs[$i] -in @("--rev", "-Rev")) { $haveRev = $true; break }
}
if (-not $haveRev) {
  $BootstrapArgs += @("-Rev", $Rev)
}

Write-Info "Running bootstrap..."
& (Join-Path $Dest "tools\\bootstrap.ps1") @BootstrapArgs
exit $LASTEXITCODE

