# Build Lumen the way the 2026-09-14 package refresh did (CHECKPOINT.md, "Package refreshed").
# Runs from the repo root regardless of the caller's directory. Stops at the first step whose
# exit code is not the expected one. Two steps exit 1 by design: the bench suites and the clean
# install are phase gates that stay red until release acceptance is complete; their component
# results are checked from the reports instead.
param([switch]$SkipEvidenceCopy)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
Set-Location $root
$wheel = 'dist/lumen_memory-0.1.0a1-py3-none-any.whl'

function Step([string]$Name, [int[]]$Expect, [scriptblock]$Command) {
    Write-Host "== $Name"
    & $Command
    if ($LASTEXITCODE -notin $Expect) { throw "$Name exited $LASTEXITCODE, expected $($Expect -join ' or ')" }
}
function Require([bool]$Condition, [string]$Message) { if (-not $Condition) { throw $Message } }
function Report([string]$Path) { Get-Content -Raw $Path | ConvertFrom-Json }

Step 'inventory (evals/dependencies.lock.json)' 0 { python tools/inventory.py }
Step 'offline tests (network denied)' 0 { python tools/offline-tests.py }
Step 'wheel from the pinned setuptools' 0 {
    python -m pip --isolated wheel --no-index --find-links artifacts/local/wheels --no-deps --wheel-dir dist .
}
Require (Test-Path $wheel) "wheel missing: $wheel"
if (-not (Test-Path .venv)) { Step 'venv' 0 { python -m venv .venv } }
Step 'reinstall into .venv' 0 {
    .venv/Scripts/python -m pip install --isolated --no-index --no-deps --force-reinstall $wheel
}
foreach ($suite in 'fixture', 'poison') {
    Step "bench --suite $suite" 1 { .venv/Scripts/lumen bench --suite $suite --output "artifacts/local/$suite.json" }
    $bench = Report "artifacts/local/$suite.json"
    Require ($bench.component_tests_passed -and $bench.runtime_unchanged) "bench ${suite}: component tests failed"
}
Step 'clean install from a fresh venv' 1 { python tools/verify-install.py }
$install = Report 'artifacts/local/clean-install.json'
Require ($install.commands.Count -eq 3 -and $install.commands[0].exit_code -eq 0 -and $install.commands[1].exit_code -eq 0) 'clean install or import failed'
Require ($install.verification_inputs_unchanged) 'acceptance inputs changed during verification'
Require ((Report 'artifacts/local/clean-install-release-1.json').local.passed) 'in-venv component tests failed'
Step 'reproduction kit and release manifest' 0 { python tools/package-release.py }

if (-not $SkipEvidenceCopy) {
    Write-Host '== evidence copies into docs/validation (byte-exact)'
    $date = Get-Date -Format 'yyyy-MM-dd'
    $suffix = ''; $n = 1
    while (Test-Path "docs/validation/alpha-install-$date$suffix.json") { $n++; $suffix = "-$n" }
    Copy-Item artifacts/local/clean-install.json "docs/validation/alpha-install-$date$suffix.json"
    Copy-Item artifacts/local/clean-install-release-1.json "docs/validation/alpha-install-release-1-$date$suffix.json"
    Copy-Item artifacts/local/clean-install.json docs/validation/current-alpha-clean-install.json
    Copy-Item artifacts/local/clean-install-release-1.json docs/validation/current-alpha-clean-install-release-1.json
}

$manifest = Report 'dist/release-manifest.json'
$sha = (Get-FileHash $wheel -Algorithm SHA256).Hash.ToLower()
Require ($manifest.files."dist/lumen_memory-0.1.0a1-py3-none-any.whl".sha256 -eq $sha) 'manifest names a different wheel'
Write-Host "wheel sha256 $sha"
Write-Host "runtime identity $($manifest.runtime_identity.runtime_digest)"
Write-Host "kit files $(@($manifest.files.PSObject.Properties).Count), code revision $($manifest.code_revision)"
Write-Host 'BUILD OK (release gates exit 1 by design; see CHECKPOINT.md)'
