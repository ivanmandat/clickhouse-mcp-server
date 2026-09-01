-- Пользователь под дашборды.
--
-- Главная мысль: гранты кодируют слоистость хранилища. Пользователю видны
-- только core и mart — значит, агент физически не может построить дашборд
-- на сыром или промежуточном слое, даже если очень захочет. Это дешевле и
-- надёжнее, чем объяснять ему то же самое в промпте.

DROP USER IF EXISTS dashboard;

CREATE USER dashboard
    IDENTIFIED WITH sha256_password BY '__DASHBOARD_PASSWORD__'
    -- профиль объявлен в clickhouse/users.d/dashboard_profile.xml
    SETTINGS PROFILE 'dashboard_profile';

DROP QUOTA IF EXISTS dashboard_quota;

CREATE QUOTA dashboard_quota
    KEYED BY user_name
    FOR INTERVAL 1 hour
        MAX queries = 2000,
            errors = 500,
            result_rows = 20000000,
            read_rows = 5000000000,
            execution_time = 3600
    TO dashboard;

-- ── данные: только два верхних слоя ───────────────────────────────────────────
GRANT SELECT ON core.* TO dashboard;
GRANT SELECT ON mart.* TO dashboard;

-- raw и int намеренно НЕ выдаются

-- ── интроспекция: без этого MCP не увидит схему ───────────────────────────────
GRANT SELECT ON system.databases  TO dashboard;
GRANT SELECT ON system.tables     TO dashboard;
GRANT SELECT ON system.columns    TO dashboard;
GRANT SELECT ON system.parts      TO dashboard;

-- нужен для приоритизации семантического слоя: по нему видно,
-- какие таблицы реально используются в запросах
GRANT SELECT ON system.query_log  TO dashboard;
