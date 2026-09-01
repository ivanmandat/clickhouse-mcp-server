-- Слой raw: как пришло из источника.
-- Все содержательные поля — String. Есть дубли, разнобой в регистре значений,
-- пустые строки вместо NULL. Это сделано намеренно: тестовая база должна
-- содержать ровно те ловушки, которые семантический слой обязан документировать.

DROP TABLE IF EXISTS raw.crm_customers;
CREATE TABLE raw.crm_customers
(
    _ingested_at DateTime64(3)      COMMENT 'Момент загрузки батча',
    _source      LowCardinality(String) COMMENT 'Система-источник',
    _batch_id    String             COMMENT 'Идентификатор батча загрузки',

    customer_id  String             COMMENT 'Идентификатор клиента в CRM',
    email        String,
    full_name    String,
    country      String             COMMENT 'ISO-код страны, но встречается пустая строка',
    signup_ts    String             COMMENT 'Дата регистрации строкой, формат YYYY-MM-DD HH:MM:SS',
    status       String             COMMENT 'Разнобой в регистре: ACTIVE / active / Active / CHURNED / churned',
    is_test      String             COMMENT 'Флаг тестового аккаунта: 0 / 1 / true / пустая строка'
)
ENGINE = MergeTree
ORDER BY (customer_id, _ingested_at)
COMMENT 'Сырая выгрузка клиентов из CRM. Дубли по customer_id ожидаемы — берите последнюю запись по _ingested_at.';

DROP TABLE IF EXISTS raw.shop_products;
CREATE TABLE raw.shop_products
(
    _ingested_at DateTime64(3),
    _source      LowCardinality(String),
    _batch_id    String,

    product_id   String,
    product_name String,
    category     String             COMMENT 'Категория товара',
    brand        String,
    unit_price   String             COMMENT 'Цена строкой, разделитель — точка',
    is_active    String
)
ENGINE = MergeTree
ORDER BY (product_id, _ingested_at)
COMMENT 'Сырая выгрузка каталога товаров из витрины магазина.';

DROP TABLE IF EXISTS raw.crm_orders;
CREATE TABLE raw.crm_orders
(
    _ingested_at DateTime64(3),
    _source      LowCardinality(String),
    _batch_id    String,

    order_id     String,
    customer_id  String,
    created_ts   String             COMMENT 'Момент создания заказа строкой',
    status       String             COMMENT 'paid / pending / cancelled / refunded — refunded появился позже остальных',
    channel      String             COMMENT 'web / mobile / partner',
    payment_method String,
    currency     String
)
ENGINE = MergeTree
ORDER BY (order_id, _ingested_at)
COMMENT 'Сырая выгрузка заказов из CRM.';

DROP TABLE IF EXISTS raw.crm_order_items;
CREATE TABLE raw.crm_order_items
(
    _ingested_at DateTime64(3),
    _source      LowCardinality(String),
    _batch_id    String,

    order_item_id String,
    order_id      String,
    product_id    String,
    quantity      String,
    unit_price    String            COMMENT 'Цена на момент заказа, строкой',
    discount_pct  String            COMMENT 'Скидка в процентах, 0..30'
)
ENGINE = MergeTree
ORDER BY (order_id, order_item_id)
COMMENT 'Сырые позиции заказов из CRM. Гранулярность — товар в заказе.';

DROP TABLE IF EXISTS raw.web_events;
CREATE TABLE raw.web_events
(
    _ingested_at DateTime64(3),
    _source      LowCardinality(String),
    _batch_id    String,

    event_id     String,
    customer_id  String            COMMENT 'Пустая строка для анонимных сессий',
    event_ts     String,
    event_type   String            COMMENT 'page_view / add_to_cart / checkout_start / purchase',
    product_id   String            COMMENT 'Заполнен не для всех типов событий',
    device       String
)
ENGINE = MergeTree
ORDER BY (event_ts, event_id)
COMMENT 'Сырой поток веб-событий. Используется для воронки, к заказам джойнится через customer_id.';
