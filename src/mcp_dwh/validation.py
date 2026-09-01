"""Валидация сохранённых дашбордов против актуальной схемы.

Ломаются не агенты, а сохранённые запросы. Валидатор находит их раньше
пользователей — но только среди тех, что кто-то обязался поддерживать:
дашборд без поддержки сломан легально, тратить на него прогон незачем.
"""

from __future__ import annotations

import json
from typing import Any

from .clickhouse import ClickHouse
from .dashboards import Dashboards
from .semantic import Semantic

# Статусы виджета:
#   ok            запрос компилируется, колонки на месте
#   broken        запрос не компилируется
#   drifted       компилируется, но набор колонок разошёлся с ожидаемым —
#                 самый коварный случай: график молча пустеет
#   stale_metric  метрика не найдена или помечена устаревшей
WIDGET_OK = "ok"
WIDGET_BROKEN = "broken"
WIDGET_DRIFTED = "drifted"
WIDGET_STALE_METRIC = "stale_metric"


def _widgets_of(spec: dict[str, Any]) -> list[dict[str, Any]]:
    widgets: list[dict[str, Any]] = []
    for row in spec.get("rows", []):
        widgets.extend(row.get("widgets", []))
    return widgets


def _encoding_columns(widget: dict[str, Any]) -> list[str]:
    """Какие колонки виджет реально использует для отрисовки."""
    enc = widget.get("encoding") or {}
    names: list[str] = []
    for key in ("x", "value", "label", "series", "y_axis"):
        val = enc.get(key)
        if isinstance(val, str):
            names.append(val)
    for key in ("y", "columns"):
        val = enc.get(key)
        if isinstance(val, list):
            names.extend(v for v in val if isinstance(v, str))
    return names


class Validator:
    def __init__(self, ch: ClickHouse, sem: Semantic, dash: Dashboards) -> None:
        self._ch = ch
        self._sem = sem
        self._dash = dash

    # ── один виджет ───────────────────────────────────────────────────────────
    def validate_widget(self, widget: dict[str, Any]) -> dict[str, Any]:
        widget_id = widget.get("id", "?")
        query = widget.get("query") or {}
        kind = query.get("kind")

        if widget.get("viz") == "text":
            return {"widget_id": widget_id, "status": WIDGET_OK, "error": None}

        # Виджет на метрике: сначала проверяем, что метрика вообще жива.
        # Именно здесь окупается ссылка на метрику вместо сырого SQL —
        # переименование колонки чинится одной правкой определения.
        if kind == "metric":
            name = query.get("metric", "")
            metric = self._sem.get_metric(name)
            if metric is None:
                return {
                    "widget_id": widget_id,
                    "status": WIDGET_STALE_METRIC,
                    "error": f"Метрика '{name}' не найдена",
                }
            if metric["status"] in ("stale", "orphaned"):
                return {
                    "widget_id": widget_id,
                    "status": WIDGET_STALE_METRIC,
                    "error": f"Метрика '{name}' помечена как {metric['status']}",
                }
            try:
                sql = self._sem.compile_metric(
                    name,
                    grain=query.get("grain"),
                    dimensions=query.get("dimensions"),
                )["sql"]
            except ValueError as exc:
                return {
                    "widget_id": widget_id,
                    "status": WIDGET_STALE_METRIC,
                    "error": str(exc),
                }
            declared = None
        elif kind == "sql":
            sql = query.get("sql", "")
            declared = query.get("declared_columns")
        else:
            return {
                "widget_id": widget_id,
                "status": WIDGET_BROKEN,
                "error": f"Неизвестный вид запроса: {kind!r}",
            }

        result = self._ch.validate(sql)
        if not result["valid"]:
            return {
                "widget_id": widget_id,
                "status": WIDGET_BROKEN,
                "error": (result["error"] or "").split("\n")[0][:400],
            }

        actual = {c["name"] for c in result["columns"]}

        # Объявленные колонки: запрос выполняется, но вернул не то.
        if declared:
            expected = {c["name"] for c in declared if isinstance(c, dict)}
            missing = sorted(expected - actual)
            if missing:
                return {
                    "widget_id": widget_id,
                    "status": WIDGET_DRIFTED,
                    "error": "Запрос выполняется, но не вернул колонки: "
                    + ", ".join(missing),
                }

        # Колонки, на которые ссылается отрисовка.
        missing_enc = sorted(set(_encoding_columns(widget)) - actual)
        if missing_enc:
            return {
                "widget_id": widget_id,
                "status": WIDGET_DRIFTED,
                "error": "Отрисовка ссылается на отсутствующие колонки: "
                + ", ".join(missing_enc),
            }

        return {"widget_id": widget_id, "status": WIDGET_OK, "error": None}

    # ── один дашборд ──────────────────────────────────────────────────────────
    def validate_dashboard(self, uid: str) -> dict[str, Any]:
        record = self._dash.get(uid)
        if record is None:
            raise ValueError(f"Дашборд '{uid}' не найден")

        spec = record["spec"]
        if isinstance(spec, str):
            spec = json.loads(spec)

        results = [self.validate_widget(w) for w in _widgets_of(spec)]
        broken = [r for r in results if r["status"] != WIDGET_OK]
        status = "broken" if broken else "ok"

        with self._dash._cursor() as cur:  # noqa: SLF001 — общий пул с dashboards
            cur.execute("DELETE FROM sem.widget_validation WHERE uid = %s", (uid,))
            for r in results:
                cur.execute(
                    """
                    INSERT INTO sem.widget_validation (uid, widget_id, status, error)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (uid, r["widget_id"], r["status"], r["error"]),
                )
            cur.execute(
                """
                UPDATE sem.dashboards
                   SET validation_status = %s,
                       last_validated_at = now(),
                       -- broken_since ставим один раз и держим, пока не починят:
                       -- по нему видно, сколько поломка живёт
                       broken_since = CASE
                           WHEN %s = 'broken' THEN coalesce(broken_since, now())
                           ELSE NULL
                       END
                 WHERE uid = %s
                """,
                (status, status, uid),
            )

        return {
            "uid": uid,
            "maintenance": record["maintenance"],
            "status": status,
            "widgets": results,
            "broken_count": len(broken),
        }

    # ── прогон по всем ────────────────────────────────────────────────────────
    def validate_all(self, only_maintained: bool = True) -> dict[str, Any]:
        """Фоновый прогон.

        only_maintained=True по умолчанию: в этом и смысл флага поддержки —
        стоимость прогона растёт с числом дашбордов, и платить за разовые
        отчёты незачем.
        """
        listing = self._dash.list(
            maintenance="maintained" if only_maintained else None,
            include_archived=False,
            limit=1000,
        )
        checked: list[dict[str, Any]] = []
        for row in listing["dashboards"]:
            checked.append(self.validate_dashboard(row["uid"]))

        broken = [c for c in checked if c["status"] == "broken"]
        skipped = 0
        if only_maintained:
            everything = self._dash.list(include_archived=True, limit=1000)
            skipped = len(everything["dashboards"]) - len(checked)

        return {
            "checked": len(checked),
            "broken": len(broken),
            "skipped_not_maintained": skipped,
            "broken_dashboards": [
                {"uid": c["uid"], "broken_widgets": c["broken_count"]} for c in broken
            ],
        }
