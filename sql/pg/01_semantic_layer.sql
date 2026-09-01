-- Семантический слой и хранилище дашбордов.
-- Живёт ПОВЕРХ физической схемы ClickHouse, а не вместо неё: физическую правду
-- агент всегда берёт интроспекцией, здесь лежит только смысл.

CREATE SCHEMA IF NOT EXISTS sem;

-- ── общие домены ──────────────────────────────────────────────────────────────
-- origin и status — сердце всей конструкции: они позволяют автоматике
-- дописывать слой, ни разу не затерев ручную работу.
DO $$ BEGIN
    CREATE TYPE sem.origin_t AS ENUM ('auto', 'agent_suggested', 'human');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE sem.status_t AS ENUM ('draft', 'verified', 'stale', 'orphaned');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Слой хранилища. Определяет пригодность таблицы к использованию в дашбордах
-- и совпадает с тем, что физически разрешено грантами пользователя dashboard.
DO $$ BEGIN
    CREATE TYPE sem.layer_t AS ENUM ('raw', 'int', 'core', 'mart');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Уровень поддержки дашборда. Не булев флаг: каждое значение меняет поведение
-- системы, а не только надпись в списке.
--
--   maintained    валидируется, поломка попадает в очередь починки, виден
--   unmaintained  НЕ валидируется, о поломке не сообщаем, виден с пометкой
--   archived      не валидируется, скрыт из выдачи по умолчанию
--
-- Стоимость цикла самопочинки растёт линейно с числом сохранённых дашбордов.
-- Без этого различения разовые отчёты забивают очередь ревью шумом.
DO $$ BEGIN
    CREATE TYPE sem.maintenance_t AS ENUM ('maintained', 'unmaintained', 'archived');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ── таблицы ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sem.tables (
    id            bigserial PRIMARY KEY,
    database_name text NOT NULL,
    table_name    text NOT NULL,
    layer         sem.layer_t,
    description   text,
    usage_notes   text,          -- «сырые данные, для отчётов брать mart.obt_sales»
    grain         text,          -- «одна строка = одна позиция заказа»
    origin        sem.origin_t NOT NULL DEFAULT 'auto',
    status        sem.status_t NOT NULL DEFAULT 'draft',
    updated_at    timestamptz  NOT NULL DEFAULT now(),
    updated_by    text,
    UNIQUE (database_name, table_name)
);

CREATE TABLE IF NOT EXISTS sem.columns (
    id           bigserial PRIMARY KEY,
    table_id     bigint NOT NULL REFERENCES sem.tables(id) ON DELETE CASCADE,
    column_name  text NOT NULL,
    description  text,
    -- top-N значений, форматы, предупреждения вида «'ACTIVE', не 'active'»
    value_hints  jsonb NOT NULL DEFAULT '{}'::jsonb,
    origin       sem.origin_t NOT NULL DEFAULT 'auto',
    status       sem.status_t NOT NULL DEFAULT 'draft',
    updated_at   timestamptz  NOT NULL DEFAULT now(),
    updated_by   text,
    UNIQUE (table_id, column_name)
);

-- ── метрики ───────────────────────────────────────────────────────────────────
-- Самая ценная часть слоя: виджет, построенный на метрике, переживает
-- переименование колонки без участия агента — правится одна эта запись.
CREATE TABLE IF NOT EXISTS sem.metrics (
    id           bigserial PRIMARY KEY,
    name         text UNIQUE NOT NULL,          -- 'revenue', 'orders', 'aov'
    display_name text,
    description  text,
    -- SQL-выражение агрегата, например: sum(net_amount * is_paid)
    sql_expression text NOT NULL,
    base_table   text NOT NULL,                 -- 'mart.obt_sales'
    time_column  text,                          -- 'order_date'
    dimensions   jsonb NOT NULL DEFAULT '[]'::jsonb,
    filters      text,                          -- обязательное условие метрики
    unit         text,
    origin       sem.origin_t NOT NULL DEFAULT 'auto',
    status       sem.status_t NOT NULL DEFAULT 'draft',
    updated_at   timestamptz  NOT NULL DEFAULT now(),
    updated_by   text
);

-- ── связи между таблицами ─────────────────────────────────────────────────────
-- В ClickHouse нет внешних ключей: это единственное место, где агент
-- узнаёт, как джойнить.
CREATE TABLE IF NOT EXISTS sem.relations (
    id            bigserial PRIMARY KEY,
    from_table    text NOT NULL,
    from_columns  text[] NOT NULL,
    to_table      text NOT NULL,
    to_columns    text[] NOT NULL,
    relation_type text NOT NULL DEFAULT 'many_to_one',
    notes         text,
    origin        sem.origin_t NOT NULL DEFAULT 'auto',
    status        sem.status_t NOT NULL DEFAULT 'draft',
    updated_at    timestamptz  NOT NULL DEFAULT now(),
    UNIQUE (from_table, to_table, from_columns, to_columns)
);

-- ── снапшоты физической схемы ─────────────────────────────────────────────────
-- Фундамент механизма обновления: дифф двух снапшотов порождает черновики.
CREATE TABLE IF NOT EXISTS sem.schema_snapshots (
    id       bigserial PRIMARY KEY,
    taken_at timestamptz NOT NULL DEFAULT now(),
    snapshot jsonb NOT NULL          -- срез system.columns целиком
);

-- ── дашборды ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sem.dashboards (
    uid               text PRIMARY KEY,
    title             text NOT NULL,
    spec              jsonb NOT NULL,
    spec_version      text NOT NULL DEFAULT '1',
    origin            sem.origin_t NOT NULL DEFAULT 'agent_suggested',
    validation_status text NOT NULL DEFAULT 'unknown',   -- ok | broken | unknown
    last_validated_at timestamptz,

    -- Поддержка по умолчанию НЕ включена: агент отвечает на разовые вопросы
    -- гораздо чаще, чем строит дашборды для команды. Поддержка — осознанный
    -- выбор, а не побочный эффект сохранения.
    maintenance            sem.maintenance_t NOT NULL DEFAULT 'unmaintained',
    maintenance_reason     text,
    maintenance_changed_at timestamptz,
    maintenance_changed_by text,

    -- Момент, когда дашборд впервые сломался и остался несломанным.
    -- Нужен для мягкого предложения снять с поддержки: если сломан давно и
    -- никто не починил — вероятно, он никому не нужен.
    broken_since      timestamptz,

    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS dashboards_maintenance_idx
    ON sem.dashboards (maintenance, validation_status);

CREATE TABLE IF NOT EXISTS sem.dashboard_versions (
    uid        text   NOT NULL REFERENCES sem.dashboards(uid) ON DELETE CASCADE,
    version    int    NOT NULL,
    spec       jsonb  NOT NULL,
    author     text,
    note       text,               -- «починка после переименования revenue»
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (uid, version)
);

CREATE TABLE IF NOT EXISTS sem.widget_validation (
    uid        text NOT NULL,
    widget_id  text NOT NULL,
    status     text NOT NULL,      -- ok | broken | drifted | stale_metric
    error      text,
    checked_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (uid, widget_id)
);

-- Анализ влияния перед правкой метрики: какие дашборды её используют.
CREATE INDEX IF NOT EXISTS dashboards_spec_gin
    ON sem.dashboards USING gin (spec jsonb_path_ops);

CREATE INDEX IF NOT EXISTS tables_layer_status_idx ON sem.tables (layer, status);
CREATE INDEX IF NOT EXISTS metrics_status_idx      ON sem.metrics (status);
