"""Общие фикстуры.

Тесты НЕ должны знать предметную область. Имена таблиц, колонок и метрик
добываются интроспекцией — ровно так же, как это делает агент. Иначе смена
домена в хранилище ломает тесты, хотя продуктовый код её переживает: именно
это и случилось при переходе с продаж на репетиторство.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

from mcp_dwh.clickhouse import ClickHouse
from mcp_dwh.config import ClickHouseConfig, _secret, load_config
from mcp_dwh.dashboards import Dashboards
from mcp_dwh.db import make_pool
from mcp_dwh.semantic import Semantic

load_dotenv()


@pytest.fixture(scope="session")
def cfg():
    try:
        return load_config()
    except RuntimeError as exc:
        pytest.skip(str(exc))


@pytest.fixture(scope="session")
def ch(cfg) -> ClickHouse:
    """Подключение под пользователем дашбордов: видит только core и mart."""
    client = ClickHouse(cfg.clickhouse)
    try:
        client.query("SELECT 1")
    except Exception:  # noqa: BLE001
        pytest.skip("ClickHouse недоступен — поднимите контейнер")
    yield client
    client.close()


@pytest.fixture(scope="session")
def ch_admin() -> ClickHouse:
    """Административное подключение. Нужно только для поиска скрытых слоёв."""
    # тот же механизм, что в продуктовом коде: файл или переменная
    password = _secret("CH_ADMIN_PASSWORD")
    if not password:
        pytest.skip("CH_ADMIN_PASSWORD (или _FILE) не задан")
    client = ClickHouse(
        ClickHouseConfig(
            host=os.getenv("CH_HOST", "127.0.0.1"),
            port=int(os.getenv("CH_HTTP_PORT", "8123")),
            user=os.getenv("CH_ADMIN_USER", "default"),
            password=password,
            allowed_databases=("raw", "int", "core", "mart"),
        )
    )
    try:
        client.query("SELECT 1")
    except Exception:  # noqa: BLE001
        pytest.skip("ClickHouse недоступен")
    yield client
    client.close()


@pytest.fixture(scope="session")
def pg_pool(cfg):
    """Один пул на сессию — как в бою: Semantic и Dashboards его делят."""
    try:
        pool = make_pool(cfg.postgres)
    except Exception:  # noqa: BLE001
        pytest.skip("Postgres недоступен")
    yield pool
    pool.close()


@pytest.fixture(scope="session")
def sem(cfg, pg_pool) -> Semantic:
    layer = Semantic(cfg.postgres, pool=pg_pool)
    try:
        layer.search_metrics("", 1)
    except Exception:  # noqa: BLE001
        pytest.skip("Postgres недоступен")
    yield layer
    layer.close()


@pytest.fixture(scope="session")
def dash(cfg, pg_pool) -> Dashboards:
    store = Dashboards(cfg.postgres, pool=pg_pool)
    try:
        store.list(limit=1)
    except Exception:  # noqa: BLE001
        pytest.skip("Postgres недоступен")
    yield store
    store.close()


# ── обнаружение объектов вместо жёстких имён ──────────────────────────────────

@pytest.fixture(scope="session")
def mart_table(ch) -> str:
    """Самая крупная таблица слоя mart."""
    res = ch.query(
        "SELECT concat(database, '.', name) FROM system.tables "
        "WHERE database = 'mart' AND total_rows > 0 "
        "ORDER BY total_rows DESC LIMIT 1"
    )
    if not res.rows:
        pytest.skip("В слое mart нет заполненных таблиц")
    return res.rows[0][0]


@pytest.fixture(scope="session")
def mart_columns(ch, mart_table) -> list[tuple[str, str]]:
    database, _, name = mart_table.partition(".")
    res = ch.query(
        f"SELECT name, type FROM system.columns "
        f"WHERE database = '{database}' AND table = '{name}' ORDER BY position",
        limit=500,
    )
    return [(r[0], r[1]) for r in res.rows]


@pytest.fixture(scope="session")
def any_column(mart_columns) -> str:
    return mart_columns[0][0]


@pytest.fixture(scope="session")
def numeric_column(mart_columns) -> str:
    for name, type_ in mart_columns:
        if any(t in type_ for t in ("Int", "Float", "Decimal")):
            return name
    pytest.skip("В таблице нет числовых колонок")


@pytest.fixture(scope="session")
def date_column(mart_columns) -> str:
    for name, type_ in mart_columns:
        if type_.startswith("Date"):
            return name
    pytest.skip("В таблице нет колонки даты")


@pytest.fixture(scope="session")
def metric_name(sem) -> str:
    """Любая проверенная метрика текущего домена."""
    metrics = [
        m for m in sem.search_metrics("", 50)["metrics"] if m["status"] == "verified"
    ]
    if not metrics:
        pytest.skip("Нет проверенных метрик")
    return metrics[0]["name"]


@pytest.fixture(scope="session")
def time_metric_name(sem) -> str:
    """Метрика с колонкой времени — для проверки разбивки по периодам."""
    for m in sem.search_metrics("", 50)["metrics"]:
        if m["status"] != "verified":
            continue
        full = sem.get_metric(m["name"])
        if full and full["time_column"]:
            return m["name"]
    pytest.skip("Нет метрик с колонкой времени")


@pytest.fixture(scope="session")
def hidden_table(ch_admin) -> str:
    """Реальная таблица из закрытого слоя.

    Нужна именно существующая: ClickHouse отдаёт ACCESS_DENIED только для
    существующих таблиц, а для отсутствующих — UNKNOWN_TABLE, и тест на
    закрытость слоя стал бы бессмысленным.
    """
    res = ch_admin.query(
        "SELECT concat(database, '.', name) FROM system.tables "
        "WHERE database IN ('raw', 'int') AND total_rows > 0 "
        "ORDER BY total_rows DESC LIMIT 1"
    )
    if not res.rows:
        pytest.skip("В закрытых слоях нет таблиц")
    return res.rows[0][0]
