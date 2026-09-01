"""Тесты слоя доступа к ClickHouse.

Имена таблиц и колонок берутся из фикстур-обнаружения, а не пишутся руками:
тест не должен знать предметную область, иначе смена домена в хранилище ломает
его, хотя продуктовый код её переживает.
"""

from __future__ import annotations

import pytest

from mcp_dwh.clickhouse import ClickHouse, QueryRejected


# ── защита до отправки на сервер ──────────────────────────────────────────────

@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO some.table VALUES (1)",
        "ALTER TABLE some.table DROP COLUMN x",
        "DROP TABLE some.table",
        "  create table x (a UInt8) engine = Memory",
        "SET max_execution_time = 9999",
    ],
)
def test_отклоняет_не_select(ch: ClickHouse, sql: str) -> None:
    with pytest.raises(QueryRejected):
        ch.query(sql)


def test_отклоняет_пустой_запрос(ch: ClickHouse) -> None:
    with pytest.raises(QueryRejected):
        ch.query("   ")


# ── лимиты выдачи ─────────────────────────────────────────────────────────────
# system.numbers намеренно не используется: у пользователя дашбордов нет на неё
# гранта, и это правильно — расширять права ради тестов не нужно.

def test_limit_добавляется_автоматически(ch, mart_table, any_column) -> None:
    res = ch.query(f"SELECT {any_column} FROM {mart_table}", limit=10)
    assert res.row_count == 10
    assert res.truncated is True


def test_свой_limit_не_перетирается(ch, mart_table, any_column) -> None:
    res = ch.query(f"SELECT {any_column} FROM {mart_table} LIMIT 3", limit=100)
    assert res.row_count == 3
    assert res.truncated is False


def test_limit_не_превышает_потолок(ch, mart_table, any_column) -> None:
    res = ch.query(f"SELECT {any_column} FROM {mart_table}", limit=10_000_000)
    assert res.row_count <= ch.config.max_limit


# ── валидация ─────────────────────────────────────────────────────────────────

def test_валидный_запрос_отдаёт_колонки(ch, mart_table, numeric_column) -> None:
    r = ch.validate(f"SELECT sum({numeric_column}) AS total FROM {mart_table}")
    assert r["valid"] is True
    assert [c["name"] for c in r["columns"]] == ["total"]


def test_ловит_несуществующую_колонку(ch, mart_table) -> None:
    """EXPLAIN SYNTAX это пропускал — проверка должна разрешать имена."""
    r = ch.validate(f"SELECT nonexistent_col FROM {mart_table}")
    assert r["valid"] is False
    assert r["error"]


def test_ловит_несуществующую_таблицу(ch) -> None:
    r = ch.validate("SELECT 1 FROM mart.no_such_table")
    assert r["valid"] is False


def test_запись_отклоняется_до_отправки(ch, mart_table) -> None:
    r = ch.validate(f"INSERT INTO {mart_table} VALUES (1)")
    assert r["valid"] is False
    assert "SELECT" in r["error"]


# ── слоистость закрыта грантами ───────────────────────────────────────────────

def test_нижние_слои_недоступны(ch, hidden_table) -> None:
    """raw и int закрыты грантами независимо от того, что в них лежит."""
    r = ch.validate(f"SELECT count() FROM {hidden_table}")
    assert r["valid"] is False
    assert "Not enough privileges" in r["error"] or "ACCESS_DENIED" in r["error"]


def test_интроспекция_видит_только_разрешённые_слои(ch) -> None:
    res = ch.query(
        "SELECT DISTINCT database FROM system.tables "
        "WHERE database NOT IN ('system', 'INFORMATION_SCHEMA', 'information_schema')"
    )
    assert {r[0] for r in res.rows} <= set(ch.config.allowed_databases)
