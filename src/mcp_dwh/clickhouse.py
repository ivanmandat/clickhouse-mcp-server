"""Доступ к ClickHouse.

Ограничения намеренно НЕ реализуются разбором SQL: readonly, таймауты и квоты
живут на стороне сервера, под пользователем dashboard. Здесь только то, чего
сервер сделать не может — обрезка выдачи, чтобы не топить контекст агента.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from .config import ClickHouseConfig
from .pool import ClientPool

# Запросы, которые агент не должен даже пытаться выполнить. Сервер их всё равно
# отклонит (readonly=1), но понятная ошибка полезнее, чем ACCESS_DENIED.
_FORBIDDEN = re.compile(
    r"^\s*(INSERT|ALTER|CREATE|DROP|TRUNCATE|RENAME|ATTACH|DETACH|GRANT|REVOKE|SET|OPTIMIZE|SYSTEM)\b",
    re.IGNORECASE,
)
_HAS_LIMIT = re.compile(r"\bLIMIT\s+\d+", re.IGNORECASE)


class QueryRejected(RuntimeError):
    """Запрос отклонён до отправки на сервер."""


@dataclass
class QueryResult:
    columns: list[str]
    types: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    elapsed_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "columns": self.columns,
            "types": self.types,
            "rows": self.rows,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "elapsed_ms": self.elapsed_ms,
        }


class ClickHouse:
    """Доступ к ClickHouse через пул клиентов.

    Пул обязателен, а не желателен: сессия clickhouse-connect допускает ровно
    один запрос за раз, и при HTTP-транспорте общий клиент разваливается на
    параллельных вызовах.
    """

    def __init__(self, cfg: ClickHouseConfig, pool_size: int | None = None) -> None:
        self._cfg = cfg
        self._pool = ClientPool(self._new_client, size=pool_size or cfg.pool_size)

    @property
    def config(self) -> ClickHouseConfig:
        return self._cfg

    @property
    def pool_stats(self) -> dict[str, int]:
        return self._pool.stats

    def _new_client(self) -> Client:
        return clickhouse_connect.get_client(
            host=self._cfg.host,
            port=self._cfg.port,
            username=self._cfg.user,
            password=self._cfg.password,
            # обязателен, когда база на другом сервере
            secure=self._cfg.secure,
            verify=self._cfg.verify,
            # Настройки не передаём: их задаёт профиль dashboard_profile,
            # а constraints не дадут поднять лимиты изнутри запроса.
        )

    def close(self) -> None:
        self._pool.close()

    # ── выполнение ────────────────────────────────────────────────────────────
    def query(self, sql: str, limit: int | None = None) -> QueryResult:
        sql = sql.strip().rstrip(";")
        if not sql:
            raise QueryRejected("Пустой запрос")
        if _FORBIDDEN.match(sql):
            raise QueryRejected(
                "Разрешены только SELECT-запросы: пользователь работает в режиме readonly"
            )

        effective = limit or self._cfg.default_limit
        effective = min(effective, self._cfg.max_limit)

        # LIMIT добавляем, только если его нет: иначе сломаем агрегаты с LIMIT BY
        wrapped = sql if _HAS_LIMIT.search(sql) else f"{sql}\nLIMIT {effective + 1}"

        import time

        started = time.monotonic()
        with self._pool.acquire() as client:
            result = client.query(wrapped)
        elapsed_ms = int((time.monotonic() - started) * 1000)

        rows = [list(r) for r in result.result_rows]
        truncated = len(rows) > effective
        if truncated:
            rows = rows[:effective]

        return QueryResult(
            columns=list(result.column_names),
            types=[str(t) for t in result.column_types],
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=elapsed_ms,
        )

    def validate(self, sql: str) -> dict[str, Any]:
        """Проверка запроса без чтения данных — основа валидации дашбордов.

        Оборачиваем запрос в подзапрос с LIMIT 0 вместо EXPLAIN SYNTAX:
        последний проверяет только синтаксис и спокойно пропускает
        несуществующую колонку. Обёртка заставляет сервер разрешить имена
        и заодно отдаёт набор колонок, которые вернёт запрос.

        Набор колонок нужен, чтобы ловить тихую поломку: запрос по-прежнему
        выполняется, но возвращает не то, что ждёт виджет, и график пустеет
        без единой ошибки.
        """
        sql = sql.strip().rstrip(";")
        if not sql:
            return {"valid": False, "error": "Пустой запрос", "columns": []}
        if _FORBIDDEN.match(sql):
            return {
                "valid": False,
                "error": "Разрешены только SELECT-запросы",
                "columns": [],
            }
        # Ошибку ловим ВНУТРИ acquire: неверный SQL не портит соединение,
        # и выбрасывать клиента из пула из-за него не нужно.
        with self._pool.acquire() as client:
            try:
                result = client.query(f"SELECT * FROM (\n{sql}\n) LIMIT 0")
            except Exception as exc:  # noqa: BLE001 — текст ошибки и есть результат
                return {"valid": False, "error": str(exc), "columns": []}
        return {
            "valid": True,
            "error": None,
            "columns": [
                {"name": n, "type": str(t)}
                for n, t in zip(result.column_names, result.column_types)
            ],
        }
