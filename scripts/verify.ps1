<#
.SYNOPSIS
    EPIC 0 kabul kontrollerini çalıştırır (Windows / PowerShell).

.DESCRIPTION
    Sırasıyla backend lint, type check, test ve migration adımlarını çalıştırır.
    Backend sanal ortamının aktif olduğu varsayılır.

.EXAMPLE
    .\scripts\verify.ps1
    .\scripts\verify.ps1 -SkipFrontend
#>
[CmdletBinding()]
param(
    [switch]$SkipFrontend
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $repoRoot 'backend'
$frontend = Join-Path $repoRoot 'frontend'
$failures = @()

function Invoke-Step {
    param(
        [string]$Name,
        [string]$WorkingDirectory,
        [string]$Command,
        [string[]]$Arguments
    )

    Write-Host "==> $Name" -ForegroundColor Cyan
    Push-Location $WorkingDirectory
    try {
        & $Command @Arguments
        if ($LASTEXITCODE -ne 0) {
            $script:failures += $Name
            Write-Host "    BAŞARISIZ ($Name), çıkış kodu $LASTEXITCODE" -ForegroundColor Red
        }
    }
    finally {
        Pop-Location
    }
}

Invoke-Step -Name 'backend lint (ruff check)' -WorkingDirectory $backend -Command 'ruff' -Arguments @('check', '.')
Invoke-Step -Name 'backend type check (mypy)' -WorkingDirectory $backend -Command 'mypy' -Arguments @()
Invoke-Step -Name 'backend test (pytest)' -WorkingDirectory $backend -Command 'pytest' -Arguments @()
Invoke-Step -Name 'backend migration (alembic upgrade head)' -WorkingDirectory $backend -Command 'alembic' -Arguments @('upgrade', 'head')

if (-not $SkipFrontend) {
    Invoke-Step -Name 'frontend type check (tsc)' -WorkingDirectory $frontend -Command 'npm' -Arguments @('run', 'typecheck')
    Invoke-Step -Name 'frontend test (vitest)' -WorkingDirectory $frontend -Command 'npm' -Arguments @('test')
    Invoke-Step -Name 'frontend build (vite)' -WorkingDirectory $frontend -Command 'npm' -Arguments @('run', 'build')
}

# Güvenlik gate'inin kendi regresyon testleri. Ağ gerektirmez (tek istisna,
# kapalı bir porta yapılan yerel "erişilemeyen registry" denemesidir).
Invoke-Step -Name 'security gate test (node --test)' -WorkingDirectory $repoRoot -Command 'node' -Arguments @('--test', 'scripts/tests/*.test.mjs')

if ($failures.Count -gt 0) {
    Write-Host ''
    Write-Host "Başarısız adımlar: $($failures -join ', ')" -ForegroundColor Red
    exit 1
}

Write-Host ''
Write-Host 'Bütün kontroller geçti.' -ForegroundColor Green
