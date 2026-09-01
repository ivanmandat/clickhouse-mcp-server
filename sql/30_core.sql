-- Слой core: очищенные витрины для аналитиков.
-- Здесь уже отфильтрованы тестовые аккаунты и посчитаны суммы позиций,
-- но метрик как таковых нет — их определяет семантический слой поверх.

DROP TABLE IF EXISTS core.dim_customer;
CREATE TABLE core.dim_customer
(
    customer_id  UInt64,
    email        String,
    full_name    String,
    country      LowCardinality(String),
    signup_at    DateTime,
    signup_date  Date,
    status       LowCardinality(String) COMMENT 'active / churned'
)
ENGINE = MergeTree
ORDER BY customer_id
COMMENT 'Измерение: клиенты. Тестовые аккаунты уже исключены.';

DROP TABLE IF EXISTS core.dim_product;
CREATE TABLE core.dim_product
(
    product_id   UInt64,
    product_name String,
    category     LowCardinality(String),
    brand        LowCardinality(String),
    unit_price   Decimal(12, 2) COMMENT 'Текущая цена каталога, не цена продажи',
    is_active    UInt8
)
ENGINE = MergeTree
ORDER BY product_id
COMMENT 'Измерение: товары.';

DROP TABLE IF EXISTS core.fct_order;
CREATE TABLE core.fct_order
(
    order_id       UInt64,
    customer_id    UInt64,
    created_at     DateTime,
    order_date     Date,
    status         LowCardinality(String) COMMENT 'paid / pending / cancelled / refunded',
    channel        LowCardinality(String),
    payment_method LowCardinality(String),
    currency       LowCardinality(String),
    items_count    UInt32         COMMENT 'Число позиций в заказе',
    gross_amount   Decimal(14, 2) COMMENT 'Сумма позиций до скидки',
    discount_amount Decimal(14, 2),
    net_amount     Decimal(14, 2) COMMENT 'Сумма после скидки. Выручкой считается только при status = paid'
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(order_date)
ORDER BY (order_date, order_id)
COMMENT 'Факт: заказ целиком. Заказы тестовых клиентов исключены. Отменённые и возвращённые оставлены — фильтруйте по status.';

DROP TABLE IF EXISTS core.fct_order_item;
CREATE TABLE core.fct_order_item
(
    order_item_id  String,
    order_id       UInt64,
    customer_id    UInt64,
    product_id     UInt64,
    created_at     DateTime,
    order_date     Date,
    order_status   LowCardinality(String),
    quantity       UInt32,
    unit_price     Decimal(12, 2),
    discount_pct   Decimal(5, 2),
    gross_amount   Decimal(14, 2) COMMENT 'quantity * unit_price',
    discount_amount Decimal(14, 2),
    net_amount     Decimal(14, 2) COMMENT 'gross_amount - discount_amount'
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(order_date)
ORDER BY (order_date, order_id, product_id)
COMMENT 'Факт: позиция заказа. Самая детальная гранулярность продаж.';
