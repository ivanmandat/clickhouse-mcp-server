﻿<#
    Разворачивает локальную тестовую среду ClickHouse целиком:
    контейнер → слои raw/int/core/mart → тестовые данные → пользователь дашбордов.

    Идемпотентен: можно запускать повторно, слои пересоздаются с нуля.

        powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1

    Флаги:
        -SkipSeed    только структура и пользователь, без генерации данных
        -Recreate    удалить том с данными и начать с чистого листа
#>
[CmdletBinding()]
param(
    [switch]$SkipSeed,
    [switch]$Recreate
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

function Write-Step { param([string]$m) Write-Host "`n=== $m" -ForegroundColor Cyan }
function Write-Ok   { param([string]$m) Write-Host "  OK  $m" -ForegroundColor Green }
function Write-Warn { param([string]$m) Write-Host "  !   $m" -ForegroundColor Yellow }

# ── .env ──────────────────────────────────────────────────────────────────────
$envPath = Join-Path $root '.env'
if (-not (Test-Path $envPath)) { throw "Не найден $envPath" }

$cfg = @{}
foreach ($line in Get-Content $envPath) {
    if ($line -match '^\s*#') { continue }
    if ($line -notmatch '=')  { continue }
    $parts = $line -split '=', 2
    $cfg[$parts[0].Trim()] = $parts[1].Trim()
}
$container = $cfg['CH_CONTAINER']
$dashUser  = $cfg['CH_DASHBOARD_USER']
$httpPort  = $cfg['CH_HTTP_PORT']

# Пароли живут в secrets/, а не в .env: тот же источник, что и у контейнеров
function Get-Secret { param([string]$Name)
    $p = Join-Path $root "secrets\$Name"
    if (-not (Test-Path $p)) { throw "Не найден секрет $p" }
    return (Get-Content $p -Raw).Trim()
}
$adminPwd = Get-Secret 'ch_admin_password'
$dashPwd  = Get-Secret 'ch_dashboard_password'

# ── проверка демона ───────────────────────────────────────────────────────────
Write-Step 'Проверяю Docker'
docker info --format '{{.ServerVersion}}' | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host @"

Демон Docker недоступен.

На этой машине служба WSL отключена (код Wsl/0x80070422), а Docker Desktop
использует WSL2-бэкенд. Выполните ОДИН раз в PowerShell от администратора:

    Set-Service -Name WSLService -StartupType Manual
    Start-Service -Name WSLService
    Start-Service -Name com.docker.service

затем запустите Docker Desktop и повторите этот скрипт.

"@ -ForegroundColor Yellow
    throw 'Docker daemon недоступен'
}
Write-Ok 'демон отвечает'

# ── контейнер ─────────────────────────────────────────────────────────────────
Push-Location $root
try {
    if ($Recreate) {
        Write-Step 'Удаляю прежний контейнер и том'
        docker compose down -v
    }

    Write-Step 'Поднимаю ClickHouse'
    docker compose up -d
    if ($LASTEXITCODE -ne 0) { throw 'docker compose up завершился с ошибкой' }

    Write-Step 'Жду готовности сервера'
    $deadline = [DateTime]::Now.AddSeconds(180)
    $ready = $false
    while ([DateTime]::Now -lt $deadline) {
        $probe = docker exec $container clickhouse-client --password $adminPwd --query 'SELECT 1'
        if ($LASTEXITCODE -eq 0 -and $probe -eq '1') { $ready = $true; break }
        Start-Sleep -Seconds 3
    }
    if (-not $ready) {
        docker compose logs --tail 40 clickhouse
        throw 'ClickHouse не поднялся за 180 секунд'
    }
    $ver = docker exec $container clickhouse-client --password $adminPwd --query 'SELECT version()'
    Write-Ok "сервер готов, версия $ver"

    # ── подстановка пароля в скрипт пользователя ──────────────────────────────
    $usersTemplate  = Join-Path $root 'sql\70_users.sql'
    $usersGenerated = Join-Path $root 'sql\.70_users.generated.sql'
    $usersSql = (Get-Content $usersTemplate -Raw -Encoding UTF8).Replace('__DASHBOARD_PASSWORD__', $dashPwd)
    # именно WriteAllText с UTF8Encoding($false): Set-Content -Encoding UTF8
    # в PowerShell 5.1 добавляет BOM, и clickhouse-client спотыкается о него
    [System.IO.File]::WriteAllText($usersGenerated, $usersSql, (New-Object System.Text.UTF8Encoding($false)))

    # ── прогон слоёв ──────────────────────────────────────────────────────────
    $steps = @(
        @{ file = '00_databases.sql';  title = 'Базы по слоям' },
        @{ file = '10_raw.sql';        title = 'Слой raw' },
        @{ file = '20_int.sql';        title = 'Слой int' },
        @{ file = '30_core.sql';       title = 'Слой core' },
        @{ file = '40_mart.sql';       title = 'Слой mart' }
    )
    if (-not $SkipSeed) {
        $steps += @{ file = '50_seed_raw.sql'; title = 'Генерация данных в raw' }
        $steps += @{ file = '60_transform.sql'; title = 'Преобразования raw -> int -> core -> mart' }
    }
    $steps += @{ file = '.70_users.generated.sql'; title = 'Пользователь дашбордов и гранты' }

    foreach ($s in $steps) {
        Write-Step $s.title
        $sw = [Diagnostics.Stopwatch]::StartNew()
        docker exec $container clickhouse-client --password $adminPwd --queries-file "/sql/$($s.file)"
        if ($LASTEXITCODE -ne 0) { throw "Ошибка на шаге $($s.file)" }
        $sw.Stop()
        Write-Ok ("{0} за {1:n1} с" -f $s.file, $sw.Elapsed.TotalSeconds)
    }

    Remove-Item $usersGenerated -Force -ErrorAction SilentlyContinue

    # ── проверки ──────────────────────────────────────────────────────────────
    Write-Step 'Проверяю наполнение'
    $counts = docker exec $container clickhouse-client --password $adminPwd --query @'
SELECT concat(database, '.', name, ' = ', toString(total_rows))
FROM system.tables
WHERE database IN ('raw','int','core','mart') AND total_rows > 0
ORDER BY database, name
'@
    $counts -split "`n" | ForEach-Object { if ($_.Trim()) { Write-Host "      $_" } }

    Write-Step 'Проверяю пользователя дашбордов'
    $mart = docker exec $container clickhouse-client --user $dashUser --password $dashPwd --query 'SELECT count() FROM mart.obt_sales'
    if ($LASTEXITCODE -eq 0) { Write-Ok "видит mart.obt_sales: $mart строк" } else { Write-Warn 'не смог прочитать mart — проверьте гранты' }

    docker exec $container clickhouse-client --user $dashUser --password $dashPwd --query 'SELECT count() FROM raw.crm_orders' | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Ok 'слой raw закрыт, как и задумано' } else { Write-Warn 'ВНИМАНИЕ: raw доступен пользователю дашбордов' }

    docker exec $container clickhouse-client --user $dashUser --password $dashPwd --query 'CREATE TABLE mart.x (a UInt8) ENGINE = Memory' | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Ok 'DDL запрещён, readonly работает' } else { Write-Warn 'ВНИМАНИЕ: пользователь смог выполнить DDL' }

    Write-Host "`n" -NoNewline
    Write-Host 'Готово.' -ForegroundColor Green
    Write-Host "  HTTP:    http://127.0.0.1:$httpPort"
    Write-Host "  админ:   default / см. .env"
    Write-Host "  дашборды: $dashUser / см. .env  (SELECT только на core и mart)"
}
finally {
    Pop-Location
}
