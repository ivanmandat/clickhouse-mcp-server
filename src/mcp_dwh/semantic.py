"""Семантический слой: что данные значат.

Хранится в Postgres отдельно от физической схемы. Рассинхрон поэтому не ломает
работу агента, а становится видимым — записи получают статус stale/orphaned.
"""

from __future__ import annotations

import json
from typing import Any

from contextlib import contextmanager

from psycopg_pool import ConnectionPool

from .config import PostgresConfig
from .db import make_pool


class Semantic:
    def __init__(self, cfg: PostgresConfig, pool: ConnectionPool | None = None) -> None:
        # Пул передаётся снаружи, чтобы Semantic и Dashboards делили один:
        # при HTTP-транспорте запросы идут параллельно, и одно соединение
        # на объект превращается в узкое горлышко.
        self._pool = pool or make_pool(cfg)
        self._owns_pool = pool is None

    @contextmanager
    def _cursor(self):
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                yield cur

    def close(self) -> None:
        if self._owns_pool:
            self._pool.close()

    def _fetch(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    # ── метрики ───────────────────────────────────────────────────────────────
    def search_metrics(self, query: str = "", limit: int = 30) -> dict[str, Any]:
        term = (query or "").strip()
        pattern = "%" + term + "%"
        rows = self._fetch(
            """
            SELECT name, display_name, description, base_table, unit,
                   status::text AS status, origin::text AS origin
            FROM sem.metrics
            WHERE %s = ''
               OR name ILIKE %s
               OR coalesce(display_name, '') ILIKE %s
               OR coalesce(description, '') ILIKE %s
            ORDER BY (status = 'verified') DESC, name
            LIMIT %s
            """,
            (term, pattern, pattern, pattern, limit),
        )
        return {
            "metrics": rows,
            "note": (
                "Предпочитайте метрику сырому SQL: виджет на метрике переживает "
                "переименование колонки без починки — правится одно определение."
            ),
        }

    def get_metric(self, name: str) -> dict[str, Any] | None:
        rows = self._fetch(
            """
            SELECT name, display_name, description, sql_expression, base_table,
                   time_column, dimensions, filters, unit,
                   status::text AS status, origin::text AS origin, updated_at
            FROM sem.metrics WHERE name = %s
            """,
            (name,),
        )
        return rows[0] if rows else None

    _GRAIN_FN = {
        "day": "toDate",
        "week": "toMonday",
        "month": "toStartOfMonth",
        "quarter": "toStartOfQuarter",
        "year": "toStartOfYear",
    }

    def compile_metric(
        self,
        name: str,
        grain: str | None = None,
        dimensions: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Разворачивает метрику в SQL.

        Именно здесь метрика становится запросом: Grafana и любой другой таргет
        про метрики ничего не знают, компиляция всегда на стороне сервера.
        """
        metric = self.get_metric(name)
        if metric is None:
            raise ValueError("Метрика '" + name + "' не найдена")

        selects: list[str] = []
        groups: list[str] = []
        time_col = metric["time_column"]

        if grain:
            if not time_col:
                raise ValueError("У метрики нет time_column, разбивка по времени невозможна")
            bucket = self._GRAIN_FN.get(grain)
            if bucket is None:
                raise ValueError("Неизвестная гранулярность: " + grain)
            selects.append(bucket + "(" + time_col + ") AS ts")
            groups.append("ts")

        for dim in dimensions or []:
            selects.append(dim)
            groups.append(dim)

        selects.append(metric["sql_expression"] + " AS " + metric["name"])

        where: list[str] = []
        if metric["filters"]:
            where.append("(" + metric["filters"] + ")")
        if date_from and time_col:
            where.append(time_col + " >= toDate('" + date_from + "')")
        if date_to and time_col:
            where.append(time_col + " <= toDate('" + date_to + "')")

        sql = "SELECT " + ", ".join(selects) + "\nFROM " + metric["base_table"]
        if where:
            sql += "\nWHERE " + " AND ".join(where)
        if groups:
            sql += "\nGROUP BY " + ", ".join(groups)
            sql += "\nORDER BY " + ", ".join(groups)

        return {"metric": name, "sql": sql, "definition": metric}

    # ── связи между таблицами ─────────────────────────────────────────────────
    def get_relations(self, table: str | None = None) -> dict[str, Any]:
        base = """
            SELECT from_table, from_columns, to_table, to_columns,
                   relation_type, notes, status::text AS status
            FROM sem.relations
        """
        if table:
            rows = self._fetch(
                base + " WHERE from_table = %s OR to_table = %s ORDER BY from_table",
                (table, table),
            )
        else:
            rows = self._fetch(base + " ORDER BY from_table, to_table")
        return {"relations": rows}

    # ── описания таблиц и колонок ─────────────────────────────────────────────
    def get_table_semantics(self, table: str) -> dict[str, Any]:
        database, _, name = table.partition(".")
        tables = self._fetch(
            """
            SELECT id, layer::text AS layer, description, usage_notes, grain,
                   status::text AS status, origin::text AS origin
            FROM sem.tables WHERE database_name = %s AND table_name = %s
            """,
            (database, name),
        )
        if not tables:
            return {"table": table, "known": False, "columns": []}

        row = tables[0]
        columns = self._fetch(
            """
            SELECT column_name, description, value_hints,
                   status::text AS status, origin::text AS origin
            FROM sem.columns WHERE table_id = %s ORDER BY column_name
            """,
            (row["id"],),
        )
        return {
            "table": table,
            "known": True,
            "layer": row["layer"],
            "description": row["description"],
            "usage_notes": row["usage_notes"],
            "grain": row["grain"],
            "status": row["status"],
            "origin": row["origin"],
            "columns": columns,
        }

    # ── пополнение слоя ───────────────────────────────────────────────────────
    def suggest_update(
        self,
        kind: str,
        target: str,
        description: str | None = None,
        value_hints: dict | None = None,
        column: str | None = None,
    ) -> dict[str, Any]:
        """Агент дописывает то, что выяснил в процессе работы.

        Пишется с origin = 'agent_suggested' и НИКОГДА не затирает
        origin = 'human': проверенное человеком остаётся неприкосновенным.
        """
        database, _, name = target.partition(".")
        if not name:
            raise ValueError("Ожидается имя вида 'база.таблица'")

        if kind == "table":
            with self._cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sem.tables (database_name, table_name, description,
                                            origin, status)
                    VALUES (%s, %s, %s, 'agent_suggested', 'draft')
                    ON CONFLICT (database_name, table_name) DO UPDATE
                       SET description = EXCLUDED.description,
                           origin = 'agent_suggested',
                           updated_at = now()
                     WHERE sem.tables.origin <> 'human'
                    RETURNING id
                    """,
                    (database, name, description),
                )
                row = cur.fetchone()
            return self._applied(row)

        if kind == "column":
            if not column:
                raise ValueError("Для kind='column' нужен параметр column")
            with self._cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sem.tables (database_name, table_name, origin, status)
                    VALUES (%s, %s, 'agent_suggested', 'draft')
                    ON CONFLICT (database_name, table_name) DO UPDATE
                       SET updated_at = now()
                    RETURNING id
                    """,
                    (database, name),
                )
                table_id = cur.fetchone()["id"]
                cur.execute(
                    """
                    INSERT INTO sem.columns (table_id, column_name, description,
                                             value_hints, origin, status)
                    VALUES (%s, %s, %s, %s, 'agent_suggested', 'draft')
                    ON CONFLICT (table_id, column_name) DO UPDATE
                       SET description = COALESCE(EXCLUDED.description,
                                                  sem.columns.description),
                           value_hints = EXCLUDED.value_hints,
                           origin = 'agent_suggested',
                           updated_at = now()
                     WHERE sem.columns.origin <> 'human'
                    RETURNING id
                    """,
                    (table_id, column, description, json.dumps(value_hints or {})),
                )
                row = cur.fetchone()
            return self._applied(row)

        raise ValueError("kind должен быть 'table' или 'column'")

    @staticmethod
    def _applied(row: Any) -> dict[str, Any]:
        if row is not None:
            return {"applied": True, "reason": None}
        return {
            "applied": False,
            "reason": "Запись помечена origin='human' и не перезаписывается автоматикой",
        }

    def review_queue(self, limit: int = 30) -> dict[str, Any]:
        """Что ждёт проверки человеком."""
        tables = self._fetch(
            """
            SELECT database_name || '.' || table_name AS table_name,
                   layer::text AS layer, status::text AS status,
                   origin::text AS origin, description
            FROM sem.tables
            WHERE status IN ('draft', 'stale')
            ORDER BY (layer = 'mart') DESC, updated_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        metrics = self._fetch(
            """
            SELECT name, status::text AS status, origin::text AS origin, base_table
            FROM sem.metrics
            WHERE status IN ('draft', 'stale')
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return {"tables": tables, "metrics": metrics}
