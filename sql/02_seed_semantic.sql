-- Стартовое наполнение семантического слоя под тестовые данные.
--
-- Всё помечено origin='human'/status='verified': это то, что человек
-- подтвердил. Автоматика такие записи не перезаписывает — именно ради этого
-- в схеме и заведены origin и status.

-- ── описания таблиц ───────────────────────────────────────────────────────────
INSERT INTO sem.tables (database_name, table_name, layer, description, usage_notes, grain, origin, status)
VALUES
    ('mart', 'obt_sales', 'mart',
     'Единая таблица продаж: все измерения заказа, клиента и товара расплющены в одну строку.',
     'Основная таблица для дашбордов по продажам. Джойны не нужны. Выручкой считается сумма net_amount при order_status = ''paid'' — используйте готовую метрику revenue.',
     'одна строка = одна позиция заказа',
     'human', 'verified'),

    ('mart', 'daily_sales', 'mart',
     'Предагрегат продаж по дню, стране, категории и каналу. Только оплаченные заказы.',
     'Быстрый источник для трендов и длинных периодов. Если разрез укладывается в четыре измерения — берите её, а не obt_sales.',
     'день × страна × категория × канал',
     'human', 'verified'),

    ('mart', 'customer_ltv', 'mart',
     'Витрина клиентов: агрегаты жизненного цикла, выручка и давность последнего заказа.',
     'days_since_last_order считается от максимальной даты в данных, а не от текущей: витрина не меняется без перезагрузки данных.',
     'один клиент',
     'human', 'verified'),

    ('mart', 'funnel_daily', 'mart',
     'Воронка веб-событий по дням и устройствам с посчитанными конверсиями.',
     'К заказам напрямую не джойнится: события и заказы связаны только через customer_id, у 35% событий он пустой.',
     'день × устройство',
     'human', 'verified'),

    ('core', 'fct_order_item', 'core',
     'Факт продаж на уровне позиции заказа, без расплющенных измерений.',
     'Для дашбордов предпочитайте mart.obt_sales — там те же данные с уже подтянутыми измерениями.',
     'одна строка = одна позиция заказа',
     'human', 'verified'),

    ('core', 'fct_order', 'core',
     'Факт заказа целиком с предпосчитанными суммами по позициям.',
     'Заказы тестовых клиентов уже исключены. Отменённые и возвращённые оставлены — фильтруйте по status.',
     'один заказ',
     'human', 'verified'),

    ('core', 'dim_customer', 'core',
     'Измерение: клиенты. Тестовые аккаунты исключены.',
     NULL, 'один клиент', 'human', 'verified'),

    ('core', 'dim_product', 'core',
     'Измерение: товары. unit_price — текущая цена каталога, а не цена продажи.',
     'Для цены на момент заказа берите unit_price из obt_sales или fct_order_item.',
     'один товар', 'human', 'verified')
ON CONFLICT (database_name, table_name) DO NOTHING;

-- ── подсказки по значениям ────────────────────────────────────────────────────
-- Ровно те ловушки, на которых агент ошибается чаще всего.
INSERT INTO sem.columns (table_id, column_name, description, value_hints, origin, status)
SELECT t.id, v.column_name, v.description, v.hints::jsonb, 'human', 'verified'
FROM sem.tables t
JOIN (VALUES
    ('mart', 'obt_sales', 'order_status',
     'Статус заказа. Выручкой считаются только строки со значением paid.',
     '{"values": ["paid", "pending", "cancelled", "refunded"], "note": "Значение refunded появилось в источнике только с июля 2024 — на более ранних периодах его нет вовсе.", "case": "нижний регистр"}'),

    ('mart', 'obt_sales', 'is_paid',
     'Признак оплаченного заказа, 1 или 0. Удобно для sum(net_amount * is_paid).',
     '{"values": [0, 1]}'),

    ('mart', 'obt_sales', 'net_amount',
     'Сумма позиции после скидки. Основная мера выручки.',
     '{"note": "Суммировать только при order_status = ''paid'', иначе в выручку попадут отменённые и возвращённые заказы."}'),

    ('mart', 'obt_sales', 'customer_country',
     'Страна клиента, ISO-код.',
     '{"values": ["RU", "KZ", "BY", "AM", "GE", "RS", "UNKNOWN"], "note": "UNKNOWN — это пустая строка из источника, а не отдельная страна."}'),

    ('mart', 'obt_sales', 'channel',
     'Канал оформления заказа.',
     '{"values": ["web", "mobile", "partner"]}'),

    ('core', 'dim_customer', 'status',
     'Статус клиента.',
     '{"values": ["active", "churned"], "case": "нижний регистр", "note": "В сыром слое приходит вперемешку ACTIVE/active/Active — нормализация делается на слое int."}')
) AS v(db, tbl, column_name, description, hints)
  ON t.database_name = v.db AND t.table_name = v.tbl
ON CONFLICT (table_id, column_name) DO NOTHING;

-- ── метрики ───────────────────────────────────────────────────────────────────
-- Виджет, построенный на метрике, переживает переименование колонки:
-- правится одна эта строка, и все дашборды следуют за ней.
INSERT INTO sem.metrics
    (name, display_name, description, sql_expression, base_table, time_column,
     dimensions, filters, unit, origin, status)
VALUES
    ('revenue', 'Выручка',
     'Сумма позиций после скидки по оплаченным заказам. Тестовые аккаунты исключены на слое core.',
     'sum(net_amount)', 'mart.obt_sales', 'order_date',
     '["customer_country", "category", "brand", "channel", "payment_method"]'::jsonb,
     'order_status = ''paid''', 'RUB', 'human', 'verified'),

    ('gross_revenue', 'Выручка до скидок',
     'Сумма позиций до применения скидки, по оплаченным заказам.',
     'sum(gross_amount)', 'mart.obt_sales', 'order_date',
     '["customer_country", "category", "brand", "channel"]'::jsonb,
     'order_status = ''paid''', 'RUB', 'human', 'verified'),

    ('discount_amount', 'Сумма скидок',
     'Сколько отдано скидками по оплаченным заказам.',
     'sum(discount_amount)', 'mart.obt_sales', 'order_date',
     '["customer_country", "category", "brand"]'::jsonb,
     'order_status = ''paid''', 'RUB', 'human', 'verified'),

    ('orders', 'Заказы',
     'Число уникальных оплаченных заказов. Считается по order_id, а не по строкам: строка — это позиция.',
     'uniqExact(order_id)', 'mart.obt_sales', 'order_date',
     '["customer_country", "category", "channel", "payment_method"]'::jsonb,
     'order_status = ''paid''', 'шт', 'human', 'verified'),

    ('customers', 'Клиенты с заказами',
     'Число уникальных клиентов, сделавших оплаченный заказ.',
     'uniqExact(customer_id)', 'mart.obt_sales', 'order_date',
     '["customer_country", "category", "channel"]'::jsonb,
     'order_status = ''paid''', 'чел', 'human', 'verified'),

    ('aov', 'Средний чек',
     'Выручка, делённая на число заказов. Именно так, а не среднее по позициям.',
     'sum(net_amount) / uniqExact(order_id)', 'mart.obt_sales', 'order_date',
     '["customer_country", "category", "channel"]'::jsonb,
     'order_status = ''paid''', 'RUB', 'human', 'verified'),

    ('items_sold', 'Продано штук',
     'Суммарное количество товара в оплаченных заказах.',
     'sum(quantity)', 'mart.obt_sales', 'order_date',
     '["customer_country", "category", "brand"]'::jsonb,
     'order_status = ''paid''', 'шт', 'human', 'verified'),

    ('refund_rate', 'Доля возвратов',
     'Доля возвращённых заказов от всех. Внимание: refunded существует только с июля 2024.',
     'uniqExactIf(order_id, order_status = ''refunded'') / uniqExact(order_id)',
     'mart.obt_sales', 'order_date',
     '["customer_country", "category", "channel"]'::jsonb,
     NULL, 'доля', 'human', 'verified')
ON CONFLICT (name) DO NOTHING;

-- ── связи между таблицами ─────────────────────────────────────────────────────
-- В ClickHouse внешних ключей нет: без этих записей агент не узнает,
-- как соединять таблицы core.
INSERT INTO sem.relations
    (from_table, from_columns, to_table, to_columns, relation_type, notes, origin, status)
VALUES
    ('core.fct_order_item', ARRAY['order_id'], 'core.fct_order', ARRAY['order_id'],
     'many_to_one', 'Позиции заказа к заказу.', 'human', 'verified'),
    ('core.fct_order_item', ARRAY['customer_id'], 'core.dim_customer', ARRAY['customer_id'],
     'many_to_one', 'Позиция к клиенту.', 'human', 'verified'),
    ('core.fct_order_item', ARRAY['product_id'], 'core.dim_product', ARRAY['product_id'],
     'many_to_one', 'Позиция к товару.', 'human', 'verified'),
    ('core.fct_order', ARRAY['customer_id'], 'core.dim_customer', ARRAY['customer_id'],
     'many_to_one', 'Заказ к клиенту.', 'human', 'verified'),
    ('mart.funnel_daily', ARRAY['event_date'], 'mart.daily_sales', ARRAY['order_date'],
     'many_to_many',
     'Только по дате. Связать события с конкретными заказами нельзя: у 35% событий пустой customer_id.',
     'human', 'verified')
ON CONFLICT (from_table, to_table, from_columns, to_columns) DO NOTHING;
