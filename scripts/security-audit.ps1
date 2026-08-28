<#
.SYNOPSIS
    Backend ve frontend dependency vulnerability audit'i (Windows / PowerShell).

.DESCRIPTION
    Backend  : pip-audit, backend/requirements.lock.txt üzerinde (PyPI Advisory DB + OSV)
    Frontend : npm audit, frontend/package-lock.json üzerinde (GitHub Advisory DB)

    Frontend değerlendirmesi scripts/npm-audit-gate.mjs tarafından yapılır;
    aynı gate scripts/security-audit.sh tarafından da kullanılır ve
    scripts/tests/npm-audit-gate.test.mjs ile otomatik test edilir.

    FAIL-CLOSED: ağ/TLS/registry hatası, ayrıştırılamayan JSON veya beklenen
    şemanın karşılanmaması durumunda komut BAŞARISIZ olur. npm'in zafiyet
    nedeniyle exit 1 vermesi ile altyapı hatası, npm'in çıkış koduna değil
    çıktının JSON şemasına bakılarak ayrılır.

.EXAMPLE
    .\scripts\security-audit.ps1
    .\scripts\security-audit.ps1 -SkipFrontend
#>
[CmdletBinding()]
param(
    [switch]$SkipBackend,
    [switch]$SkipFrontend
)

$repoRoot = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $repoRoot 'backend'
$frontend = Join-Path $repoRoot 'frontend'
$allowPath = Join-Path $PSScriptRoot 'accepted-vulnerabilities.json'
$gatePath = Join-Path $PSScriptRoot 'npm-audit-gate.mjs'

$failures = @()

function Write-Section {
    param([string]$Text)
    Write-Host ''
    Write-Host "==> $Text" -ForegroundColor Cyan
}

# ---------------------------------------------------------------------------
# Backend: pip-audit
# ---------------------------------------------------------------------------
if (-not $SkipBackend) {
    Write-Section 'backend audit (pip-audit)'

    $pipAudit = Join-Path $backend '.venv\Scripts\pip-audit.exe'
    if (-not (Test-Path $pipAudit)) {
        $pipAudit = (Get-Command pip-audit -ErrorAction SilentlyContinue).Source
    }

    if (-not $pipAudit) {
        Write-Host '    AUDIT ALTYAPI HATASI: pip-audit bulunamadi.' -ForegroundColor Red
        Write-Host '      cd backend; pip install -e ".[audit]"' -ForegroundColor Red
        $failures += 'backend audit (pip-audit kurulu degil)'
    }
    else {
        $allow = Get-Content $allowPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $ignoreArgs = @()
        foreach ($entry in $allow.pypi) {
            $ignoreArgs += @('--ignore-vuln', $entry.id)
            Write-Host "    kabul edilmis: $($entry.id) - $($entry.status)" -ForegroundColor Yellow
        }

        Push-Location $backend
        try {
            & $pipAudit -r 'requirements.lock.txt' --strict --progress-spinner off @ignoreArgs
            if ($LASTEXITCODE -ne 0) {
                $failures += 'backend audit (pip-audit basarisiz veya bulgu var)'
                Write-Host "    BASARISIZ: pip-audit exit $LASTEXITCODE" -ForegroundColor Red
            }
        }
        finally {
            Pop-Location
        }
    }
}

# ---------------------------------------------------------------------------
# Frontend: npm audit -> npm-audit-gate.mjs
# ---------------------------------------------------------------------------
if (-not $SkipFrontend) {
    Write-Section 'frontend audit (npm audit)'

    if (-not (Test-Path $gatePath)) {
        Write-Host "    AUDIT ALTYAPI HATASI: gate bulunamadi ($gatePath)" -ForegroundColor Red
        $failures += 'frontend audit (gate dosyasi yok)'
    }
    else {
        # stdout yakalanir; npm'in cikis kodu bilerek kullanilmaz, karar
        # tamamen JSON semasina gore gate tarafindan verilir.
        Push-Location $frontend
        try {
            $raw = (npm audit --json | Out-String)
        }
        catch {
            $raw = ''
        }
        finally {
            Pop-Location
        }

        $tmp = [System.IO.Path]::GetTempFileName()
        try {
            [System.IO.File]::WriteAllText($tmp, $raw, (New-Object System.Text.UTF8Encoding($false)))
            & node $gatePath --allowlist $allowPath --frontend $frontend --input $tmp
            $gateExit = $LASTEXITCODE
        }
        finally {
            [System.IO.File]::Delete($tmp)
        }

        switch ($gateExit) {
            0 { }
            1 { $failures += 'frontend audit (kabul edilmemis bulgu veya guard ihlali)' }
            default { $failures += "frontend audit (ALTYAPI HATASI, gate exit $gateExit)" }
        }
    }
}

Write-Host ''
if ($failures.Count -gt 0) {
    Write-Host "Guvenlik audit BASARISIZ: $($failures -join ' | ')" -ForegroundColor Red
    exit 1
}

Write-Host 'Guvenlik audit gecti.' -ForegroundColor Green
