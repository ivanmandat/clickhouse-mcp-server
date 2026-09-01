"""Пул соединений к Postgres.

Один пул на процесс, общий для семантического слоя и хранилища дашбордов.
При stdio-транспорте хватало одного соединения на объект, при HTTP запросы
идут параллельно, и такое соединение становится узким горлышком.
"""

from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import PostgresConfig


def make_pool(cfg: PostgresConfig, min_size: int | None = None,
              max_size: int | None = None) -> ConnectionPool:
    """Создать пул.

    autocommit=True сохраняет прежнюю семантику: каждый вызов инструмента
    самодостаточен, длинных транзакций между запросами агента нет.
    """
    return ConnectionPool(
        conninfo=cfg.dsn,
        min_size=min_size if min_size is not None else cfg.pool_min,
        max_size=max_size if max_size is not None else cfg.pool_max,
        kwargs={"row_factory": dict_row, "autocommit": True},
        open=True,
        # переоткрываем соединение раз в час: сеть между контейнерами
        # может тихо рвать долгоживущие сессии
        max_lifetime=3600,
        timeout=30,
    )
