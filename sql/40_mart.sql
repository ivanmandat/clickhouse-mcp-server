-- Слой mart: OBT и агрегаты под дашборды.
-- Это тот слой, на который агент должен опираться по умолчанию:
-- джойны уже сделаны, гранулярность объявлена в комментарии к таблице.

DROP TABLE IF EXISTS mart.obt_sales;
CREATE TABLE mart.obt_sales
(
    -- ключи
    order_item_id   String,
    order_id        UInt64,
    customer_id     UInt64,
    product_id      UInt64,

    -- время
    order_date      Date,
    created_at      DateTime,
    order_year      UInt16,
    order_month     Date            COMMENT 'Первое число месяца заказа — удобно для группировки',

    -- измерения заказа
    order_status    LowCardinality(String) COMMENT 'paid / pending / cancelled / refunded',
    channel         LowCardinality(String) COMMENT 'web / mobile / partner',
    payment_method  LowCardinality(String),

    -- измерения клиента
    customer_country LowCardinality(String),
    customer_status  LowCardinality(String),
    signup_date      Date,

    -- измерения товара
    category        LowCardinality(String),
    brand           LowCardinality(String),
    product_name    String,

    -- меры
    quantity        UInt32,
    unit_price      Decimal(12, 2),
    discount_pct    Decimal(5, 2),
    gross_amount    Decimal(14, 2),
    discount_amount Decimal(14, 2),
    net_amount      Decimal(14, 2) COMMENT 'Выручкой считается сумма net_amount при order_status = paid',
    is_paid         UInt8          COMMENT '1, если order_status = paid — удобно для sum(net_amount * is_paid)'
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(order_date)
ORDER BY (order_date, category, customer_country, order_id)
COMMENT 'OBT продаж. Гранулярность: одна строка = одна позиция заказа. Основная таблица для дашбордов по продажам.';

DROP TABLE IF EXISTS mart.daily_sales;
CREATE TABLE mart.daily_sales
(
    order_date       Date,
    country          LowCardinality(String),
    category         LowCardinality(String),
    channel          LowCardinality(String),

    orders_count     UInt64         COMMENT 'Уникальных заказов',
    items_count      UInt64,
    customers_count  UInt64         COMMENT 'Уникальных клиентов',
    gross_amount     Decimal(18, 2),
    discount_amount  Decimal(18, 2),
    net_amount       Decimal(18, 2) COMMENT 'Только оплаченные заказы'
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(order_date)
ORDER BY (order_date, country, category, channel)
COMMENT 'Агрегат продаж по дню и трём измерениям. Только оплаченные заказы. Быстрый источник для трендов.';

DROP TABLE IF EXISTS mart.customer_ltv;
CREATE TABLE mart.customer_ltv
(
    customer_id      UInt64,
    country          LowCardinality(String),
    signup_date      Date,
    first_order_date Nullable(Date),
    last_order_date  Nullable(Date),
    orders_count     UInt32,
    net_amount_total Decimal(18, 2) COMMENT 'Суммарная выручка по оплаченным заказам',
    avg_order_value  Decimal(14, 2),
    days_since_last_order Nullable(UInt32)
)
ENGINE = MergeTree
ORDER BY customer_id
COMMENT 'Витрина клиентов: агрегаты жизненного цикла. Гранулярность — один клиент.';

DROP TABLE IF EXISTS mart.funnel_daily;
CREATE TABLE mart.funnel_daily
(
    event_date        Date,
    device            LowCardinality(String),
    page_views        UInt64,
    add_to_cart       UInt64,
    checkout_starts   UInt64,
    purchases         UInt64,
    cr_view_to_cart   Float64 COMMENT 'add_to_cart / page_views',
    cr_cart_to_purchase Float64
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(event_date)
ORDER BY (event_date, device)
COMMENT 'Воронка веб-событий по дням и устройствам.';
