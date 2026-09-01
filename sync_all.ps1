# =============================================================================
# sync_all.ps1 - Synka Gen24-Shadow: git + lokal mapp + abachus_desktop_analyze_code
# =============================================================================
# Kor fran C:\Gen24-shadow:
#   powershell -ExecutionPolicy Bypass -File .\sync_all.ps1
#
# Gor:
#   1. Kopierar shadow state-machine-filerna fran C:\Gen24-shadow
#      till C:\abachus_desktop_analyze_code\packages_state\
#   2. Committar + pushar till GitHub (origin/main)
#   3. (Valfritt) Kopierar till .123 via Y:\packages
#
# OBS: Kor ALLTID fran C:\Gen24-shadow sa att git-kommandona traffar ratt repo.
# =============================================================================

$ErrorActionPreference = "Stop"

# --- 1. Synka till abachus_desktop_analyze_code\packages_state -----------------
$src = "C:\Gen24-shadow"
$dst = "C:\abachus_desktop_analyze_code\packages_state"

# Filer som finns i BADA mapparna och ska hallas identiska.
# (40_price_provider, gen24_helpers, gen24_scripts, gen24_shadow_* och sensors/
#  ligger bara i git-repot och deployas direkt till .123 - de har ingen
#  packages_state-motsvarighet.)
$stateFiles = @(
    "10_shadow_status.yaml",
    "20_state_machine.yaml",
    "25_state_evaluator.yaml",
    "30_state_dispatcher.yaml",
    "35_economic_evaluator.yaml",
    "45_optimizer.yaml"
)

Write-Host "=== Steg 1: Synka till abachus_desktop_analyze_code\packages_state ===" -ForegroundColor Cyan
foreach ($f in $stateFiles) {
    $s = Join-Path $src $f
    $d = Join-Path $dst $f
    if (Test-Path $s) {
        Copy-Item $s $d -Force
        Write-Host "  OK  $f"
    } else {
        Write-Host "  SAKNAS i git-repo: $f" -ForegroundColor Yellow
    }
}

# --- 2. Git commit + push ------------------------------------------------------
Write-Host "`n=== Steg 2: Git commit + push ===" -ForegroundColor Cyan
Set-Location $src
git add -A
git status --short
$msg = Read-Host "Commit-meddelande (Enter = 'sync: state machine files')"
if ([string]::IsNullOrWhiteSpace($msg)) { $msg = "sync: state machine files" }
git commit -m $msg
git push origin main
Write-Host "  Push klar." -ForegroundColor Green

# --- 3. (Valfritt) Deploy till .123 --------------------------------------------
Write-Host "`n=== Steg 3: Deploy till .123 (Y:\packages) ===" -ForegroundColor Cyan
$deploy = Read-Host "Kopiera till .123? (j/n)"
if ($deploy -eq "j") {
    if (-not (Test-Path "Y:\packages")) {
        Write-Host "  Y:\packages hittades inte. Montera Y: forst." -ForegroundColor Red
    } else {
        foreach ($f in $stateFiles) {
            Copy-Item (Join-Path $src $f) "Y:\packages\$f" -Force
            Write-Host "  OK  $f -> Y:\packages"
        }
        # Prisprovider + sensorer (om de andrats)
        if (Test-Path "$src\40_price_provider.yaml") {
            Copy-Item "$src\40_price_provider.yaml" "Y:\packages\40_price_provider.yaml" -Force
            Write-Host "  OK  40_price_provider.yaml -> Y:\packages"
        }
        if (Test-Path "$src\sensors") {
            if (-not (Test-Path "Y:\packages\sensors")) { New-Item -ItemType Directory -Path "Y:\packages\sensors" -Force | Out-Null }
            Get-ChildItem "$src\sensors" -File | ForEach-Object {
                Copy-Item $_.FullName "Y:\packages\sensors\$($_.Name)" -Force
                Write-Host "  OK  sensors\$($_.Name) -> Y:\packages\sensors"
            }
        }
        Write-Host "`n  Deploy klar. Starta om HA pa .123 (UI -> Installningar -> System -> Starta om)." -ForegroundColor Green
    }
} else {
    Write-Host "  Hoppade deploy till .123."
}

Write-Host "`n=== Klart ===" -ForegroundColor Green
