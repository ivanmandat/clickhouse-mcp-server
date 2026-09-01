-- Слой int: типизация, дедупликация, нормализация значений.
-- Бизнес-логики здесь нет — только приведение к пригодному для работы виду.
-- ReplacingMergeTree по _ingested_at схлопывает повторные загрузки одной сущности.

DROP TABLE IF EXISTS `int`.customers;
CREATE TABLE `int`.customers
(
    customer_id  UInt64,
    email        String,
    full_name    String,
    country      LowCardinality(String) COMMENT 'ISO-код; пустая строка источника заменена на UNKNOWN',
    signup_at    DateTime,
    status       LowCardinality(String) COMMENT 'Приведён к нижнему регистру: active / churned',
    is_test      UInt8                  COMMENT '1 — служебный тестовый аккаунт',
    _ingested_at DateTime64(3)
)
ENGINE = ReplacingMergeTree(_ingested_at)
ORDER BY customer_id
COMMENT 'Клиенты: типизированы и дедуплицированы. Тестовые аккаунты НЕ отфильтрованы — это делает core.';

DROP TABLE IF EXISTS `int`.products;
CREATE TABLE `int`.products
(
    product_id   UInt64,
    product_name String,
    category     LowCardinality(String),
    brand        LowCardinality(String),
    unit_price   Decimal(12, 2),
    is_active    UInt8,
    _ingested_at DateTime64(3)
)
ENGINE = ReplacingMergeTree(_ingested_at)
ORDER BY product_id
COMMENT 'Каталог товаров: типизирован и дедуплицирован.';

DROP TABLE IF EXISTS `int`.orders;
CREATE TABLE `int`.orders
(
    order_id       UInt64,
    customer_id    UInt64,
    created_at     DateTime,
    status         LowCardinality(String) COMMENT 'paid / pending / cancelled / refunded',
    channel        LowCardinality(String),
    payment_method LowCardinality(String),
    currency       LowCardinality(String),
    _ingested_at   DateTime64(3)
)
ENGINE = ReplacingMergeTree(_ingested_at)
ORDER BY order_id
COMMENT 'Заказы: типизированы и дедуплицированы. Отменённые и возвращённые не отфильтрованы.';

DROP TABLE IF EXISTS `int`.order_items;
CREATE TABLE `int`.order_items
(
    order_item_id String,
    order_id      UInt64,
    product_id    UInt64,
    quantity      UInt32,
    unit_price    Decimal(12, 2),
    discount_pct  Decimal(5, 2),
    _ingested_at  DateTime64(3)
)
ENGINE = ReplacingMergeTree(_ingested_at)
ORDER BY (order_id, order_item_id)
COMMENT 'Позиции заказов: типизированы. Сумма позиции считается в core, здесь её нет намеренно.';

DROP TABLE IF EXISTS `int`.web_events;
CREATE TABLE `int`.web_events
(
    event_id    UInt64,
    customer_id Nullable(UInt64) COMMENT 'NULL для анонимных сессий',
    event_at    DateTime,
    event_type  LowCardinality(String),
    product_id  Nullable(UInt64),
    device      LowCardinality(String)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(event_at)
ORDER BY (event_at, event_id)
COMMENT 'Веб-события: типизированы, пустые строки превращены в NULL.';
