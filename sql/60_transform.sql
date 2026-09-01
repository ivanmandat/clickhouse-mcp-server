-- Преобразования по слоям. Порядок важен: int → core → mart.
-- Реализовано обычными INSERT ... SELECT, а не материализованными представлениями:
-- так пересчёт слоя воспроизводим и его легко перезапустить.

-- ═══ raw → int ════════════════════════════════════════════════════════════════

TRUNCATE TABLE IF EXISTS `int`.customers;
INSERT INTO `int`.customers
    (customer_id, email, full_name, country, signup_at, status, is_test, _ingested_at)
SELECT
    customer_id,
    email,
    full_name,
    if(country = '', 'UNKNOWN', country) AS country,
    signup_at,
    status,
    is_test,
    _ingested_at_max
FROM
(
    -- дедупликация: берём последнюю версию записи по _ingested_at.
    -- Алиас НЕ должен совпадать с именем колонки: иначе _ingested_at внутри
    -- argMax() разрешается в этот алиас и получается агрегат внутри агрегата.
    SELECT
        toUInt64(customer_id)                                  AS customer_id,
        argMax(email, _ingested_at)                            AS email,
        argMax(full_name, _ingested_at)                        AS full_name,
        argMax(country, _ingested_at)                          AS country,
        parseDateTimeBestEffort(argMax(signup_ts, _ingested_at)) AS signup_at,
        lower(argMax(status, _ingested_at))                    AS status,
        if(argMax(is_test, _ingested_at) IN ('1', 'true'), 1, 0) AS is_test,
        max(_ingested_at)                                      AS _ingested_at_max
    FROM raw.crm_customers
    GROUP BY customer_id
);

TRUNCATE TABLE IF EXISTS `int`.products;
INSERT INTO `int`.products
    (product_id, product_name, category, brand, unit_price, is_active, _ingested_at)
SELECT
    toUInt64(product_id)                                        AS product_id,
    argMax(product_name, _ingested_at)                          AS product_name,
    argMax(category, _ingested_at)                              AS category,
    argMax(brand, _ingested_at)                                 AS brand,
    toDecimal64(argMax(unit_price, _ingested_at), 2)            AS unit_price,
    if(argMax(is_active, _ingested_at) = '1', 1, 0)             AS is_active,
    max(_ingested_at)                                           AS _ingested_at_max
FROM raw.shop_products
GROUP BY product_id;

TRUNCATE TABLE IF EXISTS `int`.orders;
INSERT INTO `int`.orders
    (order_id, customer_id, created_at, status, channel, payment_method, currency, _ingested_at)
SELECT
    toUInt64(order_id)                                          AS order_id,
    toUInt64(argMax(customer_id, _ingested_at))                 AS customer_id,
    parseDateTimeBestEffort(argMax(created_ts, _ingested_at))   AS created_at,
    lower(argMax(status, _ingested_at))                         AS status,
    argMax(channel, _ingested_at)                               AS channel,
    argMax(payment_method, _ingested_at)                        AS payment_method,
    argMax(currency, _ingested_at)                              AS currency,
    max(_ingested_at)                                           AS _ingested_at_max
FROM raw.crm_orders
GROUP BY order_id;

TRUNCATE TABLE IF EXISTS `int`.order_items;
INSERT INTO `int`.order_items
    (order_item_id, order_id, product_id, quantity, unit_price, discount_pct, _ingested_at)
SELECT
    order_item_id,
    toUInt64(order_id)                AS order_id,
    toUInt64(product_id)              AS product_id,
    toUInt32(quantity)                AS quantity,
    toDecimal64(unit_price, 2)        AS unit_price,
    toDecimal64(discount_pct, 2)      AS discount_pct,
    _ingested_at
FROM raw.crm_order_items;

TRUNCATE TABLE IF EXISTS `int`.web_events;
INSERT INTO `int`.web_events
    (event_id, customer_id, event_at, event_type, product_id, device)
SELECT
    toUInt64(event_id)                     AS event_id,
    -- OrNull-варианты, потому что if() в ClickHouse вычисляет обе ветки:
    -- toUInt64('') на пустой строке упал бы даже под ложным условием
    toUInt64OrNull(customer_id)            AS customer_id,
    parseDateTimeBestEffort(event_ts)      AS event_at,
    event_type,
    toUInt64OrNull(product_id)             AS product_id,
    device
FROM raw.web_events;

-- ═══ int → core ═══════════════════════════════════════════════════════════════
-- Здесь впервые отсекаются тестовые аккаунты. Это бизнес-правило, и оно должно
-- быть записано в семантический слой: «выручка считается без is_test = 1».

TRUNCATE TABLE IF EXISTS core.dim_customer;
INSERT INTO core.dim_customer
    (customer_id, email, full_name, country, signup_at, signup_date, status)
SELECT
    customer_id, email, full_name, country,
    signup_at, toDate(signup_at) AS signup_date, status
FROM `int`.customers FINAL
WHERE is_test = 0;

TRUNCATE TABLE IF EXISTS core.dim_product;
INSERT INTO core.dim_product
    (product_id, product_name, category, brand, unit_price, is_active)
SELECT product_id, product_name, category, brand, unit_price, is_active
FROM `int`.products FINAL;

TRUNCATE TABLE IF EXISTS core.fct_order_item;
INSERT INTO core.fct_order_item
    (order_item_id, order_id, customer_id, product_id, created_at, order_date, order_status,
     quantity, unit_price, discount_pct, gross_amount, discount_amount, net_amount)
SELECT
    i.order_item_id,
    i.order_id,
    o.customer_id,
    i.product_id,
    o.created_at,
    toDate(o.created_at)                                    AS order_date,
    o.status                                                AS order_status,
    i.quantity,
    i.unit_price,
    i.discount_pct,
    toDecimal64(round(gross_f, 2), 2)                       AS gross_amount,
    toDecimal64(round(gross_f * disc_f / 100, 2), 2)        AS discount_amount,
    toDecimal64(round(gross_f - gross_f * disc_f / 100, 2), 2) AS net_amount
FROM
(
    SELECT
        order_item_id, order_id, product_id, quantity, unit_price, discount_pct,
        toFloat64(quantity) * toFloat64(unit_price) AS gross_f,
        toFloat64(discount_pct)                     AS disc_f
    FROM `int`.order_items FINAL
) AS i
INNER JOIN
(
    SELECT order_id, customer_id, created_at, status
    FROM `int`.orders FINAL
) AS o USING (order_id)
-- заказы тестовых клиентов отсекаются здесь
INNER JOIN
(
    SELECT customer_id FROM `int`.customers FINAL WHERE is_test = 0
) AS c ON c.customer_id = o.customer_id;

TRUNCATE TABLE IF EXISTS core.fct_order;
INSERT INTO core.fct_order
    (order_id, customer_id, created_at, order_date, status, channel, payment_method, currency,
     items_count, gross_amount, discount_amount, net_amount)
SELECT
    o.order_id, o.customer_id, o.created_at, toDate(o.created_at) AS order_date,
    o.status, o.channel, o.payment_method, o.currency,
    agg.items_count, agg.gross_amount, agg.discount_amount, agg.net_amount
FROM
(
    SELECT order_id, customer_id, created_at, status, channel, payment_method, currency
    FROM `int`.orders FINAL
) AS o
INNER JOIN
(
    SELECT
        order_id,
        toUInt32(count())                       AS items_count,
        toDecimal64(sum(gross_amount), 2)       AS gross_amount,
        toDecimal64(sum(discount_amount), 2)    AS discount_amount,
        toDecimal64(sum(net_amount), 2)         AS net_amount
    FROM core.fct_order_item
    GROUP BY order_id
) AS agg USING (order_id);

-- ═══ core → mart ══════════════════════════════════════════════════════════════

TRUNCATE TABLE IF EXISTS mart.obt_sales;
INSERT INTO mart.obt_sales
    (order_item_id, order_id, customer_id, product_id,
     order_date, created_at, order_year, order_month,
     order_status, channel, payment_method,
     customer_country, customer_status, signup_date,
     category, brand, product_name,
     quantity, unit_price, discount_pct, gross_amount, discount_amount, net_amount, is_paid)
SELECT
    fi.order_item_id, fi.order_id, fi.customer_id, fi.product_id,
    fi.order_date, fi.created_at,
    toYear(fi.order_date)          AS order_year,
    toStartOfMonth(fi.order_date)  AS order_month,
    fi.order_status, fo.channel, fo.payment_method,
    dc.country                     AS customer_country,
    dc.status                      AS customer_status,
    dc.signup_date,
    dp.category, dp.brand, dp.product_name,
    fi.quantity, fi.unit_price, fi.discount_pct,
    fi.gross_amount, fi.discount_amount, fi.net_amount,
    if(fi.order_status = 'paid', 1, 0) AS is_paid
FROM core.fct_order_item AS fi
INNER JOIN core.fct_order   AS fo ON fo.order_id    = fi.order_id
INNER JOIN core.dim_customer AS dc ON dc.customer_id = fi.customer_id
INNER JOIN core.dim_product  AS dp ON dp.product_id  = fi.product_id;

TRUNCATE TABLE IF EXISTS mart.daily_sales;
INSERT INTO mart.daily_sales
    (order_date, country, category, channel,
     orders_count, items_count, customers_count, gross_amount, discount_amount, net_amount)
SELECT
    order_date,
    customer_country                        AS country,
    category,
    channel,
    uniqExact(order_id)                     AS orders_count,
    count()                                 AS items_count,
    uniqExact(customer_id)                  AS customers_count,
    toDecimal64(sum(gross_amount), 2)       AS gross_amount,
    toDecimal64(sum(discount_amount), 2)    AS discount_amount,
    toDecimal64(sum(net_amount), 2)         AS net_amount
FROM mart.obt_sales
WHERE is_paid = 1
GROUP BY order_date, country, category, channel;

TRUNCATE TABLE IF EXISTS mart.customer_ltv;
INSERT INTO mart.customer_ltv
    (customer_id, country, signup_date, first_order_date, last_order_date,
     orders_count, net_amount_total, avg_order_value, days_since_last_order)
SELECT
    dc.customer_id,
    dc.country,
    dc.signup_date,
    agg.first_order_date,
    agg.last_order_date,
    ifNull(agg.orders_count, 0)                       AS orders_count,
    toDecimal64(ifNull(agg.net_total, 0), 2)          AS net_amount_total,
    toDecimal64(if(ifNull(agg.orders_count, 0) = 0, 0,
                   ifNull(agg.net_total, 0) / agg.orders_count), 2) AS avg_order_value,
    -- «сегодня» берём как максимальную дату в данных, а не реальную дату машины:
    -- иначе витрина меняется каждый день без перезагрузки данных
    CAST(dateDiff('day', agg.last_order_date,
        (SELECT max(order_date) FROM mart.obt_sales)), 'Nullable(UInt32)') AS days_since_last_order
FROM core.dim_customer AS dc
LEFT JOIN
(
    SELECT
        customer_id,
        min(order_date)          AS first_order_date,
        max(order_date)          AS last_order_date,
        toUInt32(uniqExact(order_id)) AS orders_count,
        sum(net_amount)          AS net_total
    FROM mart.obt_sales
    WHERE is_paid = 1
    GROUP BY customer_id
) AS agg USING (customer_id)
SETTINGS join_use_nulls = 1;

TRUNCATE TABLE IF EXISTS mart.funnel_daily;
INSERT INTO mart.funnel_daily
    (event_date, device, page_views, add_to_cart, checkout_starts, purchases,
     cr_view_to_cart, cr_cart_to_purchase)
SELECT
    event_date, device, page_views, add_to_cart, checkout_starts, purchases,
    if(page_views  = 0, 0, add_to_cart / page_views)  AS cr_view_to_cart,
    if(add_to_cart = 0, 0, purchases   / add_to_cart) AS cr_cart_to_purchase
FROM
(
    SELECT
        toDate(event_at)                            AS event_date,
        device,
        countIf(event_type = 'page_view')           AS page_views,
        countIf(event_type = 'add_to_cart')         AS add_to_cart,
        countIf(event_type = 'checkout_start')      AS checkout_starts,
        countIf(event_type = 'purchase')            AS purchases
    FROM `int`.web_events
    GROUP BY event_date, device
);
