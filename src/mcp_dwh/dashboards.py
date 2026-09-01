"""Хранение дашбордов и управление уровнем поддержки.

Спек — источник правды, BI-инструмент получает его push-ом. Поэтому валидатор
читает спек отсюда, а не из Grafana: обратной синхронизации нет.
"""

from __future__ import annotations

import json
from typing import Any

from contextlib import contextmanager

from psycopg_pool import ConnectionPool

from .config import PostgresConfig
from .db import make_pool

MAINTENANCE_LEVELS = ("maintained", "unmaintained", "archived")

# Сколько дней дашборд должен быть сломан, чтобы предложить снять его
# с поддержки. Никто не починил за месяц — вероятно, он никому не нужен.
STALE_BROKEN_DAYS = 30


class DashboardExists(RuntimeError):
    """Идентификатор занят другим дашбордом."""

    def __init__(self, uid: str, suggestion: str) -> None:
        self.uid = uid
        self.suggestion = suggestion
        super().__init__(
            f"Дашборд '{uid}' уже существует и принадлежит, возможно, другому "
            f"пользователю. Сохраните под свободным именем '{suggestion}' либо, "
            f"если это ваш дашборд, передайте overwrite=True."
        )


class Dashboards:
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

    # ── сохранение ────────────────────────────────────────────────────────────
    def free_uid(self, uid: str, limit: int = 50) -> str:
        """Подобрать свободный идентификатор рядом с занятым."""
        taken = {
            r["uid"]
            for r in self._fetch(
                "SELECT uid FROM sem.dashboards WHERE uid = %s OR uid LIKE %s",
                (uid, uid + "-%"),
            )
        }
        for i in range(2, limit + 2):
            candidate = f"{uid}-{i}"
            if candidate not in taken:
                return candidate
        return f"{uid}-new"

    def save(
        self,
        uid: str,
        title: str,
        spec: dict[str, Any],
        maintained: bool = False,
        note: str | None = None,
        author: str = "agent",
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Сохранить дашборд, создав новую версию спека.

        maintained=False по умолчанию: разовый отчёт не должен молча становиться
        обязательством поддерживать его вечно.

        overwrite=False по умолчанию — защита от столкновения имён. Сервер общий,
        и два агента, отвечающие на один и тот же вопрос, выбирают одно и то же
        естественное имя вроде 'revenue-overview'. Без этой проверки второй молча
        затирает дашборд первого.
        """
        if not overwrite and self.get(uid) is not None:
            raise DashboardExists(uid, self.free_uid(uid))

        level = "maintained" if maintained else "unmaintained"
        payload = json.dumps(spec, ensure_ascii=False)

        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO sem.dashboards (uid, title, spec, spec_version, origin,
                                            maintenance, maintenance_changed_at,
                                            maintenance_changed_by)
                VALUES (%s, %s, %s::jsonb, %s, 'agent_suggested', %s::sem.maintenance_t,
                        now(), %s)
                ON CONFLICT (uid) DO UPDATE
                   SET title = EXCLUDED.title,
                       spec = EXCLUDED.spec,
                       spec_version = EXCLUDED.spec_version,
                       validation_status = 'unknown',
                       updated_at = now()
                RETURNING uid, maintenance::text AS maintenance
                """,
                (uid, title, payload, str(spec.get("spec_version", "1")), level, author),
            )
            row = cur.fetchone()

            cur.execute(
                """
                INSERT INTO sem.dashboard_versions (uid, version, spec, author, note)
                SELECT %s,
                       coalesce(max(version), 0) + 1,
                       %s::jsonb, %s, %s
                FROM sem.dashboard_versions WHERE uid = %s
                RETURNING version
                """,
                (uid, payload, author, note, uid),
            )
            version = cur.fetchone()["version"]

        return {
            "uid": row["uid"],
            "version": version,
            "maintenance": row["maintenance"],
            "note": (
                "Дашборд сохранён без поддержки: он не валидируется и о его поломке "
                "не сообщается. Если он нужен команде надолго — переведите в "
                "maintained через dash_set_maintenance."
                if row["maintenance"] == "unmaintained"
                else "Дашборд на поддержке: валидируется, поломка попадёт в очередь починки."
            ),
        }

    def get(self, uid: str) -> dict[str, Any] | None:
        rows = self._fetch(
            """
            SELECT uid, title, spec, spec_version, origin::text AS origin,
                   validation_status, last_validated_at,
                   maintenance::text AS maintenance, maintenance_reason,
                   maintenance_changed_at, maintenance_changed_by,
                   broken_since, created_at, updated_at
            FROM sem.dashboards WHERE uid = %s
            """,
            (uid,),
        )
        return rows[0] if rows else None

    def list(
        self,
        maintenance: str | None = None,
        include_archived: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Список дашбордов. Архивные по умолчанию скрыты — в этом их смысл."""
        clauses: list[str] = []
        params: list[Any] = []

        if maintenance:
            if maintenance not in MAINTENANCE_LEVELS:
                raise ValueError(
                    "maintenance должен быть одним из: " + ", ".join(MAINTENANCE_LEVELS)
                )
            clauses.append("maintenance = %s::sem.maintenance_t")
            params.append(maintenance)
        elif not include_archived:
            clauses.append("maintenance <> 'archived'")

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)

        rows = self._fetch(
            f"""
            SELECT uid, title, maintenance::text AS maintenance, maintenance_reason,
                   validation_status, last_validated_at, broken_since, updated_at
            FROM sem.dashboards
            {where}
            ORDER BY (maintenance = 'maintained') DESC, updated_at DESC
            LIMIT %s
            """,
            tuple(params),
        )
        return {"dashboards": rows, "count": len(rows)}

    # ── уровень поддержки ─────────────────────────────────────────────────────
    def set_maintenance(
        self, uid: str, level: str, reason: str | None = None, actor: str = "agent"
    ) -> dict[str, Any]:
        if level not in MAINTENANCE_LEVELS:
            raise ValueError(
                "level должен быть одним из: " + ", ".join(MAINTENANCE_LEVELS)
            )

        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE sem.dashboards
                   SET maintenance = %s::sem.maintenance_t,
                       maintenance_reason = %s,
                       maintenance_changed_at = now(),
                       maintenance_changed_by = %s,
                       -- снятый с поддержки перестаёт считаться сломанным:
                       -- мы за ним больше не следим
                       broken_since = CASE WHEN %s = 'maintained'
                                           THEN broken_since ELSE NULL END,
                       updated_at = now()
                 WHERE uid = %s
                RETURNING uid, maintenance::text AS maintenance, maintenance_reason
                """,
                (level, reason, actor, level, uid),
            )
            row = cur.fetchone()

        if row is None:
            raise ValueError(f"Дашборд '{uid}' не найден")
        return dict(row)

    # ── что действительно требует внимания ────────────────────────────────────
    def broken(self, only_maintained: bool = True, limit: int = 50) -> dict[str, Any]:
        """Сломанные дашборды.

        По умолчанию только те, что на поддержке: остальные сломаны легально.
        """
        where = "validation_status = 'broken'"
        if only_maintained:
            where += " AND maintenance = 'maintained'"
        else:
            where += " AND maintenance <> 'archived'"

        rows = self._fetch(
            f"""
            SELECT d.uid, d.title, d.maintenance::text AS maintenance,
                   d.broken_since, d.last_validated_at,
                   coalesce(
                       json_agg(json_build_object('widget_id', w.widget_id,
                                                  'status', w.status,
                                                  'error', w.error)
                                ORDER BY w.widget_id)
                       FILTER (WHERE w.widget_id IS NOT NULL),
                       '[]'::json
                   ) AS widgets
            FROM sem.dashboards d
            LEFT JOIN sem.widget_validation w
                   ON w.uid = d.uid AND w.status <> 'ok'
            WHERE {where}
            GROUP BY d.uid, d.title, d.maintenance, d.broken_since, d.last_validated_at
            ORDER BY d.broken_since NULLS LAST
            LIMIT %s
            """,
            (limit,),
        )
        return {"broken": rows, "count": len(rows)}

    def unmaintain_candidates(self, days: int = STALE_BROKEN_DAYS) -> dict[str, Any]:
        """Кого предложить снять с поддержки.

        Сигнал, доступный без всякой телеметрии: дашборд сломан давно, и никто
        его не починил. Автоматически НЕ снимаем — только предлагаем: решение
        о том, что отчёт больше не нужен, человеческое.
        """
        rows = self._fetch(
            """
            SELECT uid, title, broken_since,
                   extract(day FROM now() - broken_since)::int AS broken_days
            FROM sem.dashboards
            WHERE maintenance = 'maintained'
              AND validation_status = 'broken'
              AND broken_since IS NOT NULL
              AND broken_since < now() - make_interval(days => %s)
            ORDER BY broken_since
            """,
            (days,),
        )
        return {
            "candidates": rows,
            "threshold_days": days,
            "note": (
                "Сломаны дольше порога и никем не починены. Это предложение, "
                "а не действие: снимайте с поддержки через dash_set_maintenance, "
                "предварительно спросив владельца."
            ),
        }

    # ── анализ влияния ────────────────────────────────────────────────────────
    def metric_impact(self, metric: str) -> dict[str, Any]:
        """Какие дашборды сломает изменение метрики.

        Разделено по уровню поддержки: ломать неподдерживаемый отчёт не страшно,
        а вот поддерживаемый требует внимания до правки.
        """
        # Имя метрики передаём переменной jsonpath, а не склейкой строки:
        # склейка ломается на выводе типа параметра и открывает инъекцию в путь.
        # Оператор @? умеет индекс, но не умеет переменные; дашбордов сотни,
        # так что корректность здесь важнее плана запроса.
        rows = self._fetch(
            """
            SELECT uid, title, maintenance::text AS maintenance
            FROM sem.dashboards
            WHERE jsonb_path_exists(
                      spec,
                      '$.rows[*].widgets[*].query ? (@.kind == "metric" && @.metric == $m)',
                      jsonb_build_object('m', %s::text)
                  )
            ORDER BY (maintenance = 'maintained') DESC, uid
            """,
            (metric,),
        )
        maintained = [r for r in rows if r["maintenance"] == "maintained"]
        other = [r for r in rows if r["maintenance"] != "maintained"]
        return {
            "metric": metric,
            "maintained": maintained,
            "not_maintained": other,
            "summary": (
                f"На поддержке: {len(maintained)}. "
                f"Без поддержки или в архиве: {len(other)}."
            ),
        }
