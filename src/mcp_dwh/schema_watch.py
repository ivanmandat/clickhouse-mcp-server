"""Снапшоты схемы, дифф и реакция на изменения.

Механизм, который позволяет системе пережить миграцию хранилища: снимок
system.columns → дифф с предыдущим → черновики для нового, orphaned для
пропавшего, stale для того, что на пропавшее опиралось.

Ничего не удаляется. Запись со статусом orphaned — это и есть список того,
что сломалось: по ней видно, какие метрики и дашборды затронуты.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .clickhouse import ClickHouse
from .semantic import Semantic


def _quote_list(values: tuple[str, ...]) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


class SchemaWatcher:
    def __init__(self, ch: ClickHouse, sem: Semantic) -> None:
        self._ch = ch
        self._sem = sem

    # ── снимок ────────────────────────────────────────────────────────────────
    def take_snapshot(self) -> dict[str, Any]:
        """Снять срез физической схемы и сохранить в Postgres."""
        dbs = _quote_list(self._ch.config.allowed_databases)
        res = self._ch.query(
            f"""
            SELECT database, table, name, type, comment
            FROM system.columns
            WHERE database IN ({dbs})
            ORDER BY database, table, position
            """,
            limit=50_000,
        )
        snapshot = {
            f"{r[0]}.{r[1]}": {}
            for r in res.rows
        }
        for database, table, name, type_, comment in res.rows:
            snapshot[f"{database}.{table}"][name] = {"type": type_, "comment": comment}

        with self._sem._cursor() as cur:  # noqa: SLF001
            cur.execute(
                "INSERT INTO sem.schema_snapshots (snapshot) VALUES (%s::jsonb) "
                "RETURNING id, taken_at",
                (json.dumps(snapshot, ensure_ascii=False),),
            )
            row = cur.fetchone()

        return {
            "snapshot_id": row["id"],
            "taken_at": row["taken_at"],
            "tables": len(snapshot),
            "columns": sum(len(c) for c in snapshot.values()),
        }

    def _load_snapshot(self, snapshot_id: int | None) -> tuple[int | None, dict]:
        if snapshot_id is None:
            rows = self._sem._fetch(  # noqa: SLF001
                "SELECT id, snapshot FROM sem.schema_snapshots ORDER BY id DESC LIMIT 1"
            )
        else:
            rows = self._sem._fetch(  # noqa: SLF001
                "SELECT id, snapshot FROM sem.schema_snapshots WHERE id = %s",
                (snapshot_id,),
            )
        if not rows:
            return None, {}
        return rows[0]["id"], rows[0]["snapshot"]

    # ── дифф ──────────────────────────────────────────────────────────────────
    def diff(
        self, from_id: int | None = None, to_id: int | None = None
    ) -> dict[str, Any]:
        """Сравнить два снимка. По умолчанию — два последних."""
        if to_id is None or from_id is None:
            ids = self._sem._fetch(  # noqa: SLF001
                "SELECT id FROM sem.schema_snapshots ORDER BY id DESC LIMIT 2"
            )
            if len(ids) < 2:
                return {
                    "events": [],
                    "note": "Нужно минимум два снимка. Сделайте schema_snapshot ещё раз.",
                }
            to_id = to_id or ids[0]["id"]
            from_id = from_id or ids[1]["id"]

        _, old = self._load_snapshot(from_id)
        _, new = self._load_snapshot(to_id)

        events: list[dict[str, Any]] = []

        for table in sorted(set(new) - set(old)):
            events.append({"kind": "table_added", "table": table})
        for table in sorted(set(old) - set(new)):
            events.append({"kind": "table_dropped", "table": table})

        for table in sorted(set(old) & set(new)):
            old_cols, new_cols = old[table], new[table]
            added = sorted(set(new_cols) - set(old_cols))
            dropped = sorted(set(old_cols) - set(new_cols))

            # Эвристика переименования: пропавшая колонка и появившаяся того же
            # типа. Уверенно объявляем переименованием только когда кандидат
            # ровно один — иначе одновременное добавление другой колонки того же
            # типа маскирует переименование, и угадывать нельзя.
            renamed_from: set[str] = set()
            renamed_to: set[str] = set()
            rename_hints: dict[str, list[str]] = {}

            for gone in dropped:
                candidates = [
                    c
                    for c in added
                    if c not in renamed_to
                    and new_cols[c]["type"] == old_cols[gone]["type"]
                ]
                if len(candidates) == 1:
                    events.append(
                        {
                            "kind": "column_renamed?",
                            "table": table,
                            "from": gone,
                            "to": candidates[0],
                            "type": old_cols[gone]["type"],
                        }
                    )
                    renamed_from.add(gone)
                    renamed_to.add(candidates[0])
                elif candidates:
                    # Несколько кандидатов — не гадаем, но передаём список:
                    # агенту-починщику этого хватит, чтобы предложить вариант.
                    rename_hints[gone] = candidates

            for col in added:
                if col in renamed_to:
                    continue
                events.append(
                    {
                        "kind": "column_added",
                        "table": table,
                        "column": col,
                        "type": new_cols[col]["type"],
                    }
                )
            for col in dropped:
                if col in renamed_from:
                    continue
                event: dict[str, Any] = {
                    "kind": "column_dropped",
                    "table": table,
                    "column": col,
                    "type": old_cols[col]["type"],
                }
                if col in rename_hints:
                    event["rename_candidates"] = rename_hints[col]
                events.append(event)
            for col in sorted(set(old_cols) & set(new_cols)):
                if old_cols[col]["type"] != new_cols[col]["type"]:
                    events.append(
                        {
                            "kind": "column_type_changed",
                            "table": table,
                            "column": col,
                            "from": old_cols[col]["type"],
                            "to": new_cols[col]["type"],
                        }
                    )

        return {"from_snapshot": from_id, "to_snapshot": to_id, "events": events}

    # ── реакция на дифф ───────────────────────────────────────────────────────
    def apply(self, from_id: int | None = None, to_id: int | None = None) -> dict[str, Any]:
        """Привести семантический слой в соответствие с диффом.

        Новое получает черновики, пропавшее помечается orphaned, зависящее от
        пропавшего — stale. Записи, проверенные человеком, не затираются:
        описание сохраняется, меняется только статус.
        """
        result = self.diff(from_id, to_id)
        events = result.get("events", [])
        actions: list[str] = []

        for ev in events:
            kind = ev["kind"]
            table = ev.get("table", "")
            database, _, name = table.partition(".")

            with self._sem._cursor() as cur:  # noqa: SLF001
                if kind == "table_added":
                    cur.execute(
                        """
                        INSERT INTO sem.tables (database_name, table_name, layer,
                                                origin, status)
                        VALUES (%s, %s,
                                CASE WHEN %s IN ('raw','int','core','mart')
                                     THEN %s::sem.layer_t ELSE NULL END,
                                'auto', 'draft')
                        ON CONFLICT (database_name, table_name) DO UPDATE
                           SET status = 'draft', updated_at = now()
                        """,
                        (database, name, database, database),
                    )
                    actions.append(f"черновик для новой таблицы {table}")

                elif kind == "table_dropped":
                    cur.execute(
                        """
                        UPDATE sem.tables SET status = 'orphaned', updated_at = now()
                        WHERE database_name = %s AND table_name = %s
                        """,
                        (database, name),
                    )
                    if cur.rowcount:
                        actions.append(f"таблица {table} помечена orphaned")
                    cur.execute(
                        """
                        UPDATE sem.metrics SET status = 'stale', updated_at = now()
                        WHERE base_table = %s AND status <> 'orphaned'
                        """,
                        (table,),
                    )
                    if cur.rowcount:
                        actions.append(
                            f"{cur.rowcount} метрик на {table} помечены stale"
                        )

                elif kind == "column_added":
                    cur.execute(
                        """
                        INSERT INTO sem.tables (database_name, table_name, origin, status)
                        VALUES (%s, %s, 'auto', 'draft')
                        ON CONFLICT (database_name, table_name) DO UPDATE
                           SET updated_at = now()
                        RETURNING id
                        """,
                        (database, name),
                    )
                    table_id = cur.fetchone()["id"]
                    cur.execute(
                        """
                        INSERT INTO sem.columns (table_id, column_name, origin, status)
                        VALUES (%s, %s, 'auto', 'draft')
                        ON CONFLICT (table_id, column_name) DO NOTHING
                        """,
                        (table_id, ev["column"]),
                    )
                    actions.append(f"черновик для колонки {table}.{ev['column']}")

                elif kind in ("column_dropped", "column_type_changed", "column_renamed?"):
                    column = ev.get("column") or ev.get("from", "")
                    cur.execute(
                        """
                        UPDATE sem.columns SET status = %s, updated_at = now()
                        WHERE column_name = %s AND table_id IN (
                            SELECT id FROM sem.tables
                            WHERE database_name = %s AND table_name = %s
                        )
                        """,
                        (
                            "orphaned" if kind == "column_dropped" else "stale",
                            column,
                            database,
                            name,
                        ),
                    )
                    if cur.rowcount:
                        actions.append(f"колонка {table}.{column} затронута ({kind})")
                    actions.extend(self._mark_metrics_stale(table, column))

        return {
            "from_snapshot": result.get("from_snapshot"),
            "to_snapshot": result.get("to_snapshot"),
            "events": len(events),
            "actions": actions,
            "note": (
                "Ничего не удалено. Записи orphaned — это и есть список того, что "
                "сломалось. Дальше: sem_review_queue и dash_list_broken."
            ),
        }

    def _mark_metrics_stale(self, table: str, column: str) -> list[str]:
        """Метрики, опирающиеся на затронутую колонку, помечаются stale."""
        metrics = self._sem._fetch(  # noqa: SLF001
            """
            SELECT name, sql_expression, time_column, dimensions, filters
            FROM sem.metrics
            WHERE base_table = %s AND status NOT IN ('stale', 'orphaned')
            """,
            (table,),
        )
        pattern = re.compile(rf"\b{re.escape(column)}\b")
        touched: list[str] = []

        for m in metrics:
            haystack = " ".join(
                filter(
                    None,
                    [
                        m["sql_expression"],
                        m["time_column"],
                        m["filters"],
                        json.dumps(m["dimensions"], ensure_ascii=False),
                    ],
                )
            )
            if pattern.search(haystack):
                touched.append(m["name"])

        if touched:
            with self._sem._cursor() as cur:  # noqa: SLF001
                cur.execute(
                    "UPDATE sem.metrics SET status = 'stale', updated_at = now() "
                    "WHERE name = ANY(%s)",
                    (touched,),
                )
        return [f"метрика {n} помечена stale (зависит от {table}.{column})" for n in touched]
