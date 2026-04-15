param(
    [switch]$Cd,
    [switch]$Help
)

$ErrorActionPreference = "Stop"

if ($MyInvocation.InvocationName -ne ".") {
    Write-Host "[AdaOS] Dot-source this script instead of executing it:" -ForegroundColor Yellow
    Write-Host "  . .\\tools\\slot-shell.ps1" -ForegroundColor Yellow
    Write-Host "  . .\\tools\\slot-shell.ps1 -Cd" -ForegroundColor Yellow
    exit 1
}

function Invoke-AdaosSlotShell {
    param(
        [switch]$Cd,
        [switch]$Help
    )

    if ($Help) {
        Write-Host "Usage:"
        Write-Host "  . .\\tools\\slot-shell.ps1"
        Write-Host "  . .\\tools\\slot-shell.ps1 -Cd"
        return $true
    }

    $baseDir = if ([string]::IsNullOrWhiteSpace($env:ADAOS_BASE_DIR)) {
        Join-Path $HOME ".adaos"
    }
    else {
        $env:ADAOS_BASE_DIR
    }

    $slotsRoot = Join-Path $baseDir "state/core_slots"
    $activePath = Join-Path $slotsRoot "active"
    if (!(Test-Path $activePath)) {
        Write-Error "[AdaOS] Active core slot marker not found: $activePath"
        return $false
    }

    $activeSlot = (Get-Content -Raw $activePath).Trim().ToUpperInvariant()
    if ($activeSlot -notin @("A", "B")) {
        Write-Error "[AdaOS] Invalid active core slot marker: $activeSlot"
        return $false
    }

    $slotDir = Join-Path (Join-Path $slotsRoot "slots") $activeSlot
    $manifestPath = Join-Path $slotDir "manifest.json"
    if (!(Test-Path $manifestPath)) {
        Write-Error "[AdaOS] Active slot manifest not found: $manifestPath"
        return $false
    }

    $manifest = Get-Content -Raw $manifestPath | ConvertFrom-Json -AsHashtable
    if ($manifest -isnot [System.Collections.IDictionary]) {
        Write-Error "[AdaOS] Invalid slot manifest format: $manifestPath"
        return $false
    }

    $repoDir = [string]($manifest["repo_dir"])
    if ([string]::IsNullOrWhiteSpace($repoDir)) {
        $repoDir = [string]($manifest["cwd"])
    }
    if ([string]::IsNullOrWhiteSpace($repoDir)) {
        $repoDir = Join-Path $slotDir "repo"
    }

    $venvDir = [string]($manifest["venv_dir"])
    if ([string]::IsNullOrWhiteSpace($venvDir)) {
        $venvDir = Join-Path $slotDir "venv"
    }

    $cwd = [string]($manifest["cwd"])
    if ([string]::IsNullOrWhiteSpace($cwd)) {
        $cwd = $repoDir
    }

    $envMap = @{}
    if ($manifest.ContainsKey("env") -and $manifest["env"] -is [System.Collections.IDictionary]) {
        foreach ($entry in $manifest["env"].GetEnumerator()) {
            $envMap[[string]$entry.Key] = [string]$entry.Value
        }
    }

    if (-not $envMap.ContainsKey("ADAOS_BASE_DIR")) {
        $envMap["ADAOS_BASE_DIR"] = $baseDir
    }
    if (-not $envMap.ContainsKey("ADAOS_SLOT_REPO_ROOT") -and -not [string]::IsNullOrWhiteSpace($repoDir)) {
        $envMap["ADAOS_SLOT_REPO_ROOT"] = $repoDir
    }
    $srcDir = Join-Path $repoDir "src"
    if (-not $envMap.ContainsKey("PYTHONPATH") -and (Test-Path $srcDir)) {
        $envMap["PYTHONPATH"] = $srcDir
    }

    if (Get-Command deactivate -ErrorAction SilentlyContinue) {
        try {
            deactivate | Out-Null
        }
        catch {
        }
    }

    Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue

    $activateCandidates = @(
        (Join-Path $venvDir "Scripts/Activate.ps1"),
        (Join-Path $venvDir "bin/Activate.ps1")
    )
    $activatePath = $activateCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $activatePath) {
        Write-Error "[AdaOS] Slot activation script not found under: $venvDir"
        return $false
    }

    . $activatePath

    $env:ADAOS_ACTIVE_CORE_SLOT = $activeSlot
    $env:ADAOS_ACTIVE_CORE_SLOT_DIR = $slotDir
    foreach ($key in ($envMap.Keys | Sort-Object)) {
        if ($key -in @("PATH", "VIRTUAL_ENV", "PYTHONHOME")) {
            continue
        }
        Set-Item -Path ("Env:" + $key) -Value ([string]$envMap[$key])
    }

    if ($Cd -and -not [string]::IsNullOrWhiteSpace($cwd)) {
        Set-Location $cwd
    }

    return $true
}

$script:adaosSlotShellOk = Invoke-AdaosSlotShell -Cd:$Cd -Help:$Help
Remove-Item Function:Invoke-AdaosSlotShell -ErrorAction SilentlyContinue
if (-not $script:adaosSlotShellOk) {
    return
}
