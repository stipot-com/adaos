#Requires -Version 5.1
# Minimal "download & bootstrap" entrypoint (Windows PowerShell 5.1+).
# Served from GitHub raw:
#   https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/windows/init.ps1
# Zone arguments are passed through to tools/bootstrap.ps1, for example: -ZoneId ru -Dev

[CmdletBinding(PositionalBinding = $false)]
param(
  [string]$Dest = "$HOME\\adaos",
  [string]$Rev = "rev2026",
  [string]$RepoOwner = "stipot-com",
  [string]$RepoName = "adaos",
  [string]$JoinCode = "",
  [string]$Role = "",
  [switch]$Dev,
  [switch]$NoVoice,
  [ValidateSet("auto", "always", "never")]
  [string]$InstallService = "auto",
  [string]$ServeHost = "",
  [int]$ServePort = 0,
  [int]$ControlPort = 0,
  [string]$RootUrl = "",
  [string]$ZoneId = "",
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$BootstrapArgs
)

$ErrorActionPreference = "Stop"

function Write-Info([string]$s) { Write-Host "[*] $s" -ForegroundColor Cyan }
function Write-Ok([string]$s) { Write-Host "[+] $s" -ForegroundColor Green }
function Write-Warn([string]$s) { Write-Host "[!] $s" -ForegroundColor Yellow }
function Have([string]$cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

if (-not $PSVersionTable -or -not $PSVersionTable.PSVersion) {
  throw "Unsupported PowerShell runtime: unable to detect PSVersionTable.PSVersion. Use Windows PowerShell 5.1+ or PowerShell 7+."
}
if ($PSVersionTable.PSVersion.Major -lt 5 -or ($PSVersionTable.PSVersion.Major -eq 5 -and $PSVersionTable.PSVersion.Minor -lt 1)) {
  throw "Unsupported PowerShell version $($PSVersionTable.PSVersion). This installer requires Windows PowerShell 5.1+ or PowerShell 7+."
}

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
  $extractedItems = @(Get-ChildItem -LiteralPath $extracted.FullName -Force)
  if ($extractedItems.Count -eq 0) {
    throw "Extracted directory is empty: $($extracted.FullName)"
  }

  if (Test-Path $Dest) {
    try { Remove-Item -Recurse -Force -LiteralPath $Dest } catch { }
  }
  New-Item -ItemType Directory -Force -Path $Dest | Out-Null
  foreach ($item in $extractedItems) {
    Copy-Item -Recurse -Force -LiteralPath $item.FullName -Destination $Dest
  }
  Write-Ok ("Source extracted to: {0}" -f $Dest)
}
finally {
  try { Remove-Item -Recurse -Force -LiteralPath $tmp } catch { }
}

Set-Location -LiteralPath $Dest

$bootstrapPath = Join-Path $Dest "tools\\bootstrap.ps1"
if (-not (Test-Path -LiteralPath $bootstrapPath)) {
  throw "Bootstrap script not found: $bootstrapPath"
}

$bootstrapParams = @{}
if (-not [string]::IsNullOrWhiteSpace($JoinCode)) {
  $bootstrapParams["JoinCode"] = $JoinCode
}
if (-not [string]::IsNullOrWhiteSpace($Role)) {
  $bootstrapParams["Role"] = $Role
}
if ($Dev) {
  $bootstrapParams["Dev"] = $true
}
if ($NoVoice) {
  $bootstrapParams["NoVoice"] = $true
}
if ($PSBoundParameters.ContainsKey("InstallService")) {
  $bootstrapParams["InstallService"] = $InstallService
}
if (-not [string]::IsNullOrWhiteSpace($ServeHost)) {
  $bootstrapParams["ServeHost"] = $ServeHost
}
if ($ServePort -gt 0) {
  $bootstrapParams["ServePort"] = $ServePort
}
if ($ControlPort -gt 0) {
  $bootstrapParams["ControlPort"] = $ControlPort
}
if (-not [string]::IsNullOrWhiteSpace($RootUrl)) {
  $bootstrapParams["RootUrl"] = $RootUrl
}
if (-not [string]::IsNullOrWhiteSpace($ZoneId)) {
  $bootstrapParams["ZoneId"] = $ZoneId
}
if (-not [string]::IsNullOrWhiteSpace($Rev)) {
  $bootstrapParams["Rev"] = $Rev
}

Write-Info "Running bootstrap..."
& $bootstrapPath @bootstrapParams @BootstrapArgs
exit $LASTEXITCODE
