param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

$py = Join-Path $repoRoot ".venv\\Scripts\\python.exe"
if (!(Test-Path $py)) {
    Write-Host "No .venv found. Run bootstrap first:" -ForegroundColor Yellow
    Write-Host "  powershell -ExecutionPolicy Bypass -File tools/bootstrap.ps1" -ForegroundColor Yellow
    exit 1
}

& $py -m adaos @Args
exit $LASTEXITCODE

