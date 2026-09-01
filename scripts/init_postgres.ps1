﻿<#
    Создаёт базу mcp_dwh в уже установленном PostgreSQL и накатывает
    схему семантического слоя.

        powershell -ExecutionPolicy Bypass -File scripts\init_postgres.ps1

    Пароль берётся из PG_PASSWORD в .env — в командную строку не попадает.
#>
[CmdletBinding()]
param(
    [switch]$Recreate
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

function Write-Step { param([string]$m) Write-Host "`n=== $m" -ForegroundColor Cyan }
function Write-Ok   { param([string]$m) Write-Host "  OK  $m" -ForegroundColor Green }

# ── .env ──────────────────────────────────────────────────────────────────────
$cfg = @{}
foreach ($line in Get-Content (Join-Path $root '.env')) {
    if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
    $p = $line -split '=', 2
    $cfg[$p[0].Trim()] = $p[1].Trim()
}

$pgUser = $cfg['PG_SUPERUSER']
# Пароль из secrets/, единый источник с контейнерами
$pgPassPath = Join-Path $root 'secrets\pg_password'
if (-not (Test-Path $pgPassPath)) { throw "Не найден секрет $pgPassPath" }
$pgPass = (Get-Content $pgPassPath -Raw).Trim()
$pgHost = $cfg['PG_HOST']
$pgPort = $cfg['PG_PORT']
$pgDb   = $cfg['PG_DATABASE']

if ([string]::IsNullOrWhiteSpace($pgPass)) {
    throw "Пустой секрет secrets/pg_password"
}

# ── psql ──────────────────────────────────────────────────────────────────────
$psql = Get-ChildItem 'C:\Program Files\PostgreSQL' -Recurse -Filter 'psql.exe' -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
if (-not $psql) { throw "psql.exe не найден в C:\Program Files\PostgreSQL" }
Write-Ok "psql: $($psql.FullName)"

# пароль передаём переменной окружения, а не аргументом командной строки
$env:PGPASSWORD = $pgPass

function Invoke-Psql {
    param([string]$Database, [string]$Query, [string]$File)
    $args = @('-h', $pgHost, '-p', $pgPort, '-U', $pgUser, '-d', $Database,
              '-v', 'ON_ERROR_STOP=1', '--no-psqlrc', '-q')
    if ($File)  { $args += @('-f', $File) }
    if ($Query) { $args += @('-c', $Query) }
    & $psql.FullName @args
}

try {
    Write-Step 'Проверяю подключение'
    $ver = Invoke-Psql -Database 'postgres' -Query 'SELECT version()'
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось подключиться — проверьте PG_PASSWORD в .env' }
    Write-Ok 'подключение есть'

    if ($Recreate) {
        Write-Step "Удаляю базу $pgDb"
        Invoke-Psql -Database 'postgres' -Query "DROP DATABASE IF EXISTS $pgDb"
    }

    Write-Step "Создаю базу $pgDb"
    $exists = Invoke-Psql -Database 'postgres' -Query "SELECT 1 FROM pg_database WHERE datname = '$pgDb'"
    if ($exists -match '1') {
        Write-Ok 'база уже существует'
    } else {
        Invoke-Psql -Database 'postgres' -Query "CREATE DATABASE $pgDb ENCODING 'UTF8'"
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать базу' }
        Write-Ok 'база создана'
    }

    # схема доменно-нейтральна, наполнение зависит от предметной области
    $files = @(
        (Join-Path $root 'sql\pg_semantic_layer.sql'),
        (Join-Path $root 'sql	utoring_seed_semantic.sql')
    )
    foreach ($f in $files) {
        Write-Step "Применяю $(Split-Path $f -Leaf)"
        Invoke-Psql -Database $pgDb -File $f
        if ($LASTEXITCODE -ne 0) { throw "Ошибка применения $f" }
        Write-Ok (Split-Path $f -Leaf)
    }

    Write-Step 'Проверяю'
    Invoke-Psql -Database $pgDb -Query @"
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'sem' ORDER BY table_name
"@

    Write-Host "`nГотово." -ForegroundColor Green
    Write-Host "  база: $pgDb на $pgHost`:$pgPort"
}
finally {
    Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
}
