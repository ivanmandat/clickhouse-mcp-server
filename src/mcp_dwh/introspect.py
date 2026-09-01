"""Интроспекция схемы ClickHouse.

Схема добывается на лету, а не вкладывается в промпт: поэтому изменение
структуры базы не требует ни правок сервера, ни обновления инструкций агента.
"""

from __future__ import annotations

from typing import Any

from .clickhouse import ClickHouse


def _db_filter(databases: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{d}'" for d in databases)
    return f"database IN ({quoted})"


def search_tables(ch: ClickHouse, query: str = "", limit: int = 40) -> dict[str, Any]:
    """Поиск таблиц по имени, комментарию и именам колонок.

    Именно поиск, а не список: на большой базе полный перечень бесполезен.
    """
    dbs = _db_filter(ch.config.allowed_databases)
    pattern = (query or "").strip().lower()

    sql = f"""
    SELECT
        t.database,
        t.name,
        t.total_rows,
        t.comment,
        arrayStringConcat(
            arraySlice(
                arraySort(groupArray(c.name)), 1, 12
            ), ', '
        ) AS sample_columns
    FROM system.tables AS t
    LEFT JOIN system.columns AS c
        ON c.database = t.database AND c.table = t.name
    WHERE {dbs}
    GROUP BY t.database, t.name, t.total_rows, t.comment
    """
    if pattern:
        safe = pattern.replace("'", "''")
        sql += f"""
        HAVING positionCaseInsensitive(t.name, '{safe}') > 0
            OR positionCaseInsensitive(t.comment, '{safe}') > 0
            OR positionCaseInsensitive(sample_columns, '{safe}') > 0
        """
    sql += f"\nORDER BY t.database, t.name\nLIMIT {int(limit)}"

    res = ch.query(sql, limit=limit)
    return {
        "tables": [
            {
                "database": r[0],
                "table": r[1],
                "full_name": f"{r[0]}.{r[1]}",
                "rows": r[2],
                "comment": r[3] or None,
                "columns_preview": r[4],
            }
            for r in res.rows
        ],
        "query": query,
        "note": (
            "Видны только слои core и mart: raw и int закрыты грантами. "
            "Для дашбордов по умолчанию берите mart."
        ),
    }


def describe_table(ch: ClickHouse, table: str) -> dict[str, Any]:
    """Колонки, типы, комментарии и ключ сортировки.

    Ключ сортировки важен: без него агент пишет запросы мимо первичного ключа.
    """
    database, _, name = table.partition(".")
    if not name:
        raise ValueError("Ожидается имя вида 'база.таблица'")
    if database not in ch.config.allowed_databases:
        raise ValueError(
            f"База '{database}' недоступна. Разрешены: "
            f"{', '.join(ch.config.allowed_databases)}"
        )

    db_q = database.replace("'", "''")
    tb_q = name.replace("'", "''")

    meta = ch.query(
        f"""
        SELECT total_rows, comment, sorting_key, partition_key, engine
        FROM system.tables
        WHERE database = '{db_q}' AND name = '{tb_q}'
        """
    )
    if not meta.rows:
        raise ValueError(f"Таблица {table} не найдена")

    cols = ch.query(
        f"""
        SELECT name, type, comment, is_in_sorting_key, is_in_partition_key
        FROM system.columns
        WHERE database = '{db_q}' AND table = '{tb_q}'
        ORDER BY position
        """,
        limit=500,
    )

    rows_total, comment, sorting_key, partition_key, engine = meta.rows[0]
    return {
        "table": table,
        "engine": engine,
        "rows": rows_total,
        "comment": comment or None,
        "sorting_key": sorting_key or None,
        "partition_key": partition_key or None,
        "columns": [
            {
                "name": c[0],
                "type": c[1],
                "comment": c[2] or None,
                "in_sorting_key": bool(c[3]),
                "in_partition_key": bool(c[4]),
            }
            for c in cols.rows
        ],
    }


def sample_data(ch: ClickHouse, table: str, n: int = 5) -> dict[str, Any]:
    """Несколько строк: показывает реальные форматы значений."""
    database, _, name = table.partition(".")
    if database not in ch.config.allowed_databases:
        raise ValueError(f"База '{database}' недоступна")
    n = max(1, min(int(n), 50))
    res = ch.query(f"SELECT * FROM {database}.{name} LIMIT {n}", limit=n)
    return {"table": table, **res.to_dict()}


def profile_column(ch: ClickHouse, table: str, column: str, top: int = 15) -> dict[str, Any]:
    """Кардинальность, доля пустых, top-N значений.

    Без этого агент фильтрует по status = 'active', когда в базе 'ACTIVE'.
    """
    database, _, name = table.partition(".")
    if database not in ch.config.allowed_databases:
        raise ValueError(f"База '{database}' недоступна")

    col = column.replace("`", "")
    top = max(1, min(int(top), 100))

    stats = ch.query(
        f"""
        SELECT
            count()                        AS rows_total,
            uniqExact(`{col}`)             AS distinct_values,
            countIf(isNull(`{col}`))       AS nulls,
            toString(min(`{col}`))         AS min_value,
            toString(max(`{col}`))         AS max_value
        FROM {database}.{name}
        """
    )
    top_values = ch.query(
        f"""
        SELECT toString(`{col}`) AS value, count() AS cnt
        FROM {database}.{name}
        GROUP BY value
        ORDER BY cnt DESC
        LIMIT {top}
        """,
        limit=top,
    )

    rows_total, distinct, nulls, min_v, max_v = stats.rows[0]
    return {
        "table": table,
        "column": column,
        "rows": rows_total,
        "distinct_values": distinct,
        "nulls": nulls,
        "min": min_v,
        "max": max_v,
        "top_values": [{"value": r[0], "count": r[1]} for r in top_values.rows],
    }
