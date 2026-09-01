-- Наполнение слоя raw. Всё генерируется средствами самого ClickHouse:
-- никаких внешних скриптов и зависимостей.
--
-- Период: 2024-01-01 .. 2025-08-31 (609 дней), с трендом роста и просадкой выходных.
-- Цена товара выводится детерминированно из product_id, поэтому позиции заказов
-- согласованы с каталогом без джойна.

-- ── клиенты ───────────────────────────────────────────────────────────────────
INSERT INTO raw.crm_customers
SELECT
    now64(3) - toIntervalSecond(number % 3600)                                  AS _ingested_at,
    'crm'                                                                       AS _source,
    'seed-001'                                                                  AS _batch_id,
    toString(1000 + number)                                                     AS customer_id,
    concat('user', toString(1000 + number), '@',
           arrayElement(['mail.ru', 'gmail.com', 'yandex.ru', 'example.com'], toInt32(number % 4) + 1)) AS email,
    concat(arrayElement(['Иван','Пётр','Анна','Мария','Олег','Дарья','Сергей','Елена'],
                        toInt32(intHash32(number) % 8) + 1),
           ' ',
           arrayElement(['Смирнов','Иванов','Кузнецов','Попов','Волков','Соколова'],
                        toInt32(intHash32(number + 7) % 6) + 1))                AS full_name,
    -- каждая 211-я строка приходит с пустой страной
    if(number % 211 = 0, '',
       arrayElement(['RU','KZ','BY','AM','GE','RS'], toInt32(intHash32(number + 3) % 6) + 1)) AS country,
    -- toString(DateTime) даёт 'YYYY-MM-DD hh:mm:ss'.
    -- НЕ formatDateTime с %M: в ClickHouse %M — название месяца, минуты это %i
    toString(toDateTime('2023-06-01 00:00:00')
             + toIntervalSecond(intHash32(number + 11) % 63072000))            AS signup_ts,
    -- намеренный разнобой регистра: ровно та ловушка, которую должен описать value_hints
    arrayElement(['ACTIVE','active','Active','CHURNED','churned'],
                 toInt32(intHash32(number + 17) % 5) + 1)                       AS status,
    if(number % 97 = 0, '1', '0')                                               AS is_test
FROM numbers(6000);

-- повторная загрузка части клиентов: дубли по customer_id с более поздним _ingested_at
INSERT INTO raw.crm_customers
SELECT now64(3), _source, 'seed-002', customer_id, email, full_name,
       country, signup_ts, lower(status), is_test
FROM raw.crm_customers
WHERE cityHash64(customer_id) % 31 = 0;

-- ── товары ────────────────────────────────────────────────────────────────────
INSERT INTO raw.shop_products
SELECT
    now64(3) - toIntervalSecond(number % 600) AS _ingested_at,
    'shop'                                    AS _source,
    'seed-001'                                AS _batch_id,
    toString(number + 1)                      AS product_id,
    concat(brand_v, ' ', model_v, ' ', toString(number + 1)) AS product_name,
    cat_v                                     AS category,
    brand_v                                   AS brand,
    toString(round(100 + (cityHash64(toString(number + 1)) % 90000) / 100, 2)) AS unit_price,
    if(number % 23 = 0, '0', '1')             AS is_active
FROM
(
    SELECT
        number,
        arrayElement(['Alpha','Beta','Gamma','Delta','Epsilon'],
                     toInt32(intHash32(number) % 5) + 1)      AS brand_v,
        arrayElement(['Widget','Gadget','Module','Kit','Pack','Unit'],
                     toInt32(intHash32(number + 1) % 6) + 1)  AS model_v,
        arrayElement(['Electronics','Home','Sports','Beauty','Toys','Garden',
                      'Auto','Books','Grocery','Pets','Office','Fashion'],
                     toInt32(intHash32(number + 2) % 12) + 1) AS cat_v
    FROM numbers(800)
);

-- ── заказы ────────────────────────────────────────────────────────────────────
INSERT INTO raw.crm_orders
SELECT
    now64(3) - toIntervalSecond(number % 7200) AS _ingested_at,
    'crm'                                      AS _source,
    'seed-001'                                 AS _batch_id,
    toString(500000 + number)                  AS order_id,
    toString(1000 + (cityHash64(number, 'cust') % 6000)) AS customer_id,
    toString(created_dt)                       AS created_ts,
    -- refunded появился в источнике только с июля 2024 — типичная история поля,
    -- из-за которой value_hints устаревают
    if(status_v = 'refunded' AND created_dt < toDateTime('2024-07-01 00:00:00'),
       'cancelled', status_v)                  AS status,
    channel_v                                  AS channel,
    pay_v                                      AS payment_method,
    'RUB'                                      AS currency
FROM
(
    SELECT
        number,
        created_dt,
        multiIf(r < 78, 'paid',
                r < 86, 'pending',
                r < 95, 'cancelled',
                        'refunded') AS status_v,
        arrayElement(['web','web','web','mobile','mobile','partner'],
                     toInt32(cityHash64(number, 'ch') % 6) + 1) AS channel_v,
        arrayElement(['card','card','card','sbp','wallet','invoice'],
                     toInt32(cityHash64(number, 'pm') % 6) + 1) AS pay_v
    FROM
    (
        SELECT
            number,
            toDateTime('2024-01-01 00:00:00')
                -- показатель 0.7 смещает массу к недавним датам: получается тренд роста
                + toIntervalDay(toUInt32(608 * pow((cityHash64(number, 'day') % 1000000) / 1000000.0, 0.7)))
                + toIntervalSecond(cityHash64(number, 'sec') % 86400) AS created_dt,
            toInt32(cityHash64(number, 'st') % 100)                   AS r
        FROM numbers(150000)
    )
)
-- просадка выходных: убираем 35% субботних и воскресных заказов
WHERE NOT (toDayOfWeek(created_dt) IN (6, 7) AND cityHash64(number, 'wk') % 100 < 35);

-- ── позиции заказов ───────────────────────────────────────────────────────────
INSERT INTO raw.crm_order_items
SELECT
    now64(3)                                   AS _ingested_at,
    'crm'                                      AS _source,
    'seed-001'                                 AS _batch_id,
    concat(order_id, '-', toString(item_no))   AS order_item_id,
    order_id,
    toString(pid)                              AS product_id,
    toString(qty)                              AS quantity,
    toString(round(100 + (cityHash64(toString(pid)) % 90000) / 100, 2)) AS unit_price,
    toString(disc)                             AS discount_pct
FROM
(
    SELECT
        order_id,
        item_no,
        1 + (cityHash64(order_id, item_no, 'p') % 800) AS pid,
        1 + (cityHash64(order_id, item_no, 'q') % 4)   AS qty,
        arrayElement([0, 0, 0, 0, 5, 10, 10, 15, 20, 30],
                     toInt32(cityHash64(order_id, item_no, 'd') % 10) + 1) AS disc
    FROM
    (
        SELECT
            order_id,
            -- 1..4 позиции в заказе
            arrayJoin(range(toUInt32(1), toUInt32(2 + cityHash64(order_id, 'n') % 4))) AS item_no
        FROM raw.crm_orders
    )
);

-- ── веб-события ───────────────────────────────────────────────────────────────
INSERT INTO raw.web_events
SELECT
    now64(3)                          AS _ingested_at,
    'web'                             AS _source,
    'seed-001'                        AS _batch_id,
    toString(number + 1)              AS event_id,
    -- 35% событий анонимные: пустая строка вместо идентификатора
    if(cityHash64(number, 'anon') % 100 < 35, '',
       toString(1000 + (cityHash64(number, 'c') % 6000))) AS customer_id,
    toString(ev_dt)                   AS event_ts,
    ev_type,
    if(ev_type IN ('page_view', 'add_to_cart'),
       toString(1 + (cityHash64(number, 'p') % 800)), '') AS product_id,
    arrayElement(['desktop','mobile','mobile','tablet'],
                 toInt32(cityHash64(number, 'dev') % 4) + 1) AS device
FROM
(
    SELECT
        number,
        toDateTime('2024-01-01 00:00:00')
            + toIntervalDay(toUInt32(608 * pow((cityHash64(number, 'eday') % 1000000) / 1000000.0, 0.7)))
            + toIntervalSecond(cityHash64(number, 'esec') % 86400) AS ev_dt,
        multiIf(er < 62, 'page_view',
                er < 84, 'add_to_cart',
                er < 94, 'checkout_start',
                         'purchase') AS ev_type
    FROM
    (
        SELECT number, toInt32(cityHash64(number, 'et') % 100) AS er
        FROM numbers(300000)
    )
);
