#!/usr/bin/env bash
#
# Разворачивает mcp-dwh целиком на Linux-сервере: секреты, контейнеры,
# слои хранилища, семантический слой, пользователь дашбордов.
#
#   ./scripts/setup.sh              полное развёртывание
#   ./scripts/setup.sh --no-seed    только структура, без тестовых данных
#   ./scripts/setup.sh --recreate   снести тома и начать с нуля
#   ./scripts/setup.sh --domain tutoring   какой набор SQL применять
#
# Идемпотентен: повторный запуск пересоздаёт слои, но не трогает секреты.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SEED=1
RECREATE=0
DOMAIN="tutoring"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-seed)  SEED=0 ;;
        --recreate) RECREATE=1 ;;
        --domain)   DOMAIN="${2:?нужно имя домена}"; shift ;;
        -h|--help)  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Неизвестный аргумент: $1" >&2; exit 1 ;;
    esac
    shift
done

step() { printf '\n\033[36m=== %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mOK\033[0m  %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m   %s\n' "$1"; }
die()  { printf '  \033[31mОШИБКА\033[0m %s\n' "$1" >&2; exit 1; }

# ── предусловия ───────────────────────────────────────────────────────────────
step 'Проверяю окружение'
command -v docker >/dev/null || die 'docker не установлен'
docker compose version >/dev/null 2>&1 || die 'нужен docker compose v2 (плагин compose)'
docker info >/dev/null 2>&1 || die 'демон docker недоступен — нет прав или не запущен'
ok "docker $(docker version --format '{{.Server.Version}}')"

SQL_DIR="sql"
[[ "$DOMAIN" != "sales" ]] && SQL_DIR="sql/$DOMAIN"
[[ -d "$SQL_DIR" ]] || die "нет каталога $SQL_DIR — проверьте --domain"
ok "домен: $DOMAIN (SQL из $SQL_DIR)"

# ── секреты ───────────────────────────────────────────────────────────────────
step 'Секреты'
mkdir -p secrets
gen() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "${1:-24}"; }

make_secret() {
    local name="$1" value="$2"
    if [[ -s "secrets/$name" ]]; then
        ok "secrets/$name — уже есть, не трогаю"
    else
        printf '%s' "$value" > "secrets/$name"
        chmod 600 "secrets/$name"
        ok "secrets/$name — создан"
    fi
}

make_secret ch_admin_password     "$(gen 24)"
make_secret ch_dashboard_password "$(gen 24)"
make_secret gf_admin_password     "$(gen 20)"
make_secret mcp_auth_token        "$(gen 43)"
make_secret pg_password           "$(gen 24)"

# DSN собирается после пароля: сервис postgres живёт в сети compose
PG_USER="${PG_SUPERUSER:-postgres}"
PG_DB="${PG_DATABASE:-mcp_dwh}"
PG_PASS="$(cat secrets/pg_password)"
make_secret pg_dsn       "postgresql://${PG_USER}:${PG_PASS}@postgres:5432/${PG_DB}"
make_secret pg_dsn_local "postgresql://${PG_USER}:${PG_PASS}@127.0.0.1:5432/${PG_DB}"

# ── .env ──────────────────────────────────────────────────────────────────────
if [[ ! -f .env ]]; then
    step 'Создаю .env (только несекретные настройки)'
    cat > .env <<'ENVEOF'
CH_ADMIN_USER=default
CH_DASHBOARD_USER=dashboard
CH_HTTP_PORT=8123
CH_NATIVE_PORT=9000
CH_CONTAINER=mcp-clickhouse
CH_ALLOWED_DATABASES=core,mart
CH_SECURE=false
CH_DASHBOARD_PASSWORD_FILE=secrets/ch_dashboard_password
CH_ADMIN_PASSWORD_FILE=secrets/ch_admin_password

PG_SUPERUSER=postgres
PG_HOST=127.0.0.1
PG_PORT=5432
PG_DATABASE=mcp_dwh
PG_DSN_FILE=secrets/pg_dsn_local

GF_PORT=3000
GF_ADMIN_USER=admin
GF_URL=http://127.0.0.1:3000
GF_DATASOURCE_UID=clickhouse-dwh
GF_ADMIN_PASSWORD_FILE=secrets/gf_admin_password

MCP_TRANSPORT=stdio
MCP_HOST=0.0.0.0
MCP_PORT=8765
MCP_AUTH_TOKEN_FILE=secrets/mcp_auth_token
ENVEOF
    ok '.env создан'
else
    ok '.env уже есть'
fi

COMPOSE=(docker compose --profile with-postgres)

# ── контейнеры ────────────────────────────────────────────────────────────────
if [[ $RECREATE -eq 1 ]]; then
    step 'Сношу тома'
    "${COMPOSE[@]}" down -v
fi

step 'Поднимаю сервисы'
"${COMPOSE[@]}" up -d --build

wait_healthy() {
    local name="$1" tries="${2:-60}"
    for _ in $(seq 1 "$tries"); do
        local st
        st="$(docker inspect --format '{{.State.Health.Status}}' "$name" 2>/dev/null || echo missing)"
        [[ "$st" == healthy ]] && { ok "$name готов"; return 0; }
        [[ "$st" == missing ]] && { warn "$name не запущен"; return 1; }
        sleep 3
    done
    docker logs "$name" --tail 30
    die "$name не стал healthy"
}

step 'Жду готовности'
wait_healthy mcp-postgres
wait_healthy mcp-clickhouse

# ── слои хранилища ────────────────────────────────────────────────────────────
CH_ADMIN_PW="$(cat secrets/ch_admin_password)"
ch() { docker exec -i mcp-clickhouse clickhouse-client --password "$CH_ADMIN_PW" "$@"; }

run_sql() {
    local file="$1"
    [[ -f "$file" ]] || { warn "нет файла $file — пропускаю"; return 0; }
    local start; start=$(date +%s%N)
    docker exec -i mcp-clickhouse clickhouse-client --password "$CH_ADMIN_PW" --multiquery < "$file" \
        || die "ошибка в $file"
    ok "$(basename "$file") за $(( ($(date +%s%N) - start) / 1000000 )) мс"
}

step 'Слои хранилища'
run_sql sql/00_databases.sql
run_sql "$SQL_DIR/10_raw.sql"
run_sql "$SQL_DIR/20_int.sql"
run_sql "$SQL_DIR/30_core.sql"
run_sql "$SQL_DIR/40_mart.sql"

if [[ $SEED -eq 1 ]]; then
    step 'Тестовые данные'
    run_sql "$SQL_DIR/50_seed_raw.sql"
    run_sql "$SQL_DIR/60_transform.sql"
fi

step 'Пользователь дашбордов'
DASH_PW="$(cat secrets/ch_dashboard_password)"
# пароль подставляем в поток, а не в файл на диске
sed "s|__DASHBOARD_PASSWORD__|${DASH_PW}|" sql/70_users.sql \
    | docker exec -i mcp-clickhouse clickhouse-client --password "$CH_ADMIN_PW" --multiquery \
    || die 'не удалось создать пользователя'
ok 'пользователь и гранты созданы'

# ── семантический слой ────────────────────────────────────────────────────────
step 'Семантический слой'
psql_in() {
    docker exec -i -e PGPASSWORD="$PG_PASS" mcp-postgres \
        psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 -q "$@"
}
# при первом старте тома схему накатывает entrypoint postgres,
# при повторном запуске делаем это явно
for f in sql/pg/01_semantic_layer.sql "$SQL_DIR/02_seed_semantic.sql"; do
    [[ -f "$f" ]] || continue
    psql_in < "$f" && ok "$(basename "$f")"
done

step 'Поднимаю Grafana и MCP'
"${COMPOSE[@]}" up -d grafana mcp
wait_healthy mcp-grafana 80
wait_healthy mcp-server

# ── проверки ──────────────────────────────────────────────────────────────────
step 'Проверяю'
ch --query "SELECT concat(database,'.',name,' = ',toString(total_rows)) FROM system.tables WHERE database IN ('raw','int','core','mart') AND total_rows > 0 ORDER BY database, name" \
    | sed 's/^/      /'

DASH_USER="${CH_DASHBOARD_USER:-dashboard}"
if docker exec -i mcp-clickhouse clickhouse-client --user "$DASH_USER" --password "$DASH_PW" \
        --query 'SELECT 1' >/dev/null 2>&1; then
    ok 'пользователь дашбордов подключается'
else
    warn 'пользователь дашбордов не подключается'
fi

if docker exec -i mcp-clickhouse clickhouse-client --user "$DASH_USER" --password "$DASH_PW" \
        --query 'SELECT count() FROM raw.crm_lessons' >/dev/null 2>&1; then
    warn 'ВНИМАНИЕ: слой raw доступен пользователю дашбордов'
else
    ok 'слой raw закрыт, как и задумано'
fi

if curl -fsS http://127.0.0.1:"${MCP_PORT:-8765}"/health >/dev/null; then
    ok 'MCP отвечает на /health'
fi

printf '\n\033[32mГотово.\033[0m\n'
printf '  MCP      : http://127.0.0.1:%s/mcp\n' "${MCP_PORT:-8765}"
printf '  токен    : cat secrets/mcp_auth_token\n'
printf '  Grafana  : http://127.0.0.1:%s (admin / cat secrets/gf_admin_password)\n' "${GF_PORT:-3000}"
printf '  ClickHouse: http://127.0.0.1:%s\n\n' "${CH_HTTP_PORT:-8123}"
printf 'Перед выставлением наружу поставьте обратный прокси с TLS.\n'
