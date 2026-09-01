"""Адаптер рендера в Grafana.

Спек в Postgres — источник правды, Grafana получает его push-ом и является
одноразовой проекцией. Обратной синхронизации нет: правки, сделанные внутри
интерфейса Grafana, будут затёрты следующим push-ом. Именно это сохраняет
работоспособной петлю валидации — валидатор читает спек, а не Grafana.

Метрики разворачиваются в SQL здесь, на стороне сервера: Grafana про понятие
метрики ничего не знает.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .config import GrafanaConfig
from .semantic import Semantic

# Спек описывает раскладку долями из 12, у Grafana сетка 24-колоночная.
SPEC_GRID = 12
GRAFANA_GRID = 24
DEFAULT_ROW_HEIGHT = 8
STAT_ROW_HEIGHT = 4

# Портируемое подмножество типов визуализации → панели Grafana.
PANEL_TYPES = {
    "line": "timeseries",
    "area": "timeseries",
    "bar": "barchart",
    "stacked_bar": "barchart",
    "stat": "stat",
    "table": "table",
    "pie": "piechart",
    "heatmap": "heatmap",
    "text": "text",
}

# format в запросе плагина ClickHouse: 0 — временной ряд, 1 — таблица.
TIME_SERIES_VIZ = {"line", "area", "bar", "stacked_bar", "heatmap"}


class GrafanaError(RuntimeError):
    pass


class GrafanaAdapter:
    def __init__(self, cfg: GrafanaConfig, sem: Semantic) -> None:
        self._cfg = cfg
        self._sem = sem

    # ── HTTP ──────────────────────────────────────────────────────────────────
    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        import base64

        url = self._cfg.url.rstrip("/") + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        token = base64.b64encode(
            f"{self._cfg.user}:{self._cfg.password}".encode()
        ).decode()
        req.add_header("Authorization", f"Basic {token}")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise GrafanaError(f"{exc.code} {exc.reason}: {detail}") from None
        except urllib.error.URLError as exc:
            raise GrafanaError(f"Grafana недоступна: {exc.reason}") from None

        return json.loads(body) if body else None

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/api/health")

    # ── компиляция запроса виджета ────────────────────────────────────────────
    def _widget_sql(self, widget: dict[str, Any]) -> str:
        query = widget.get("query") or {}
        kind = query.get("kind")

        if kind == "sql":
            return query.get("sql", "")

        if kind == "metric":
            # Grafana не знает про метрики — разворачиваем здесь
            return self._sem.compile_metric(
                query.get("metric", ""),
                grain=query.get("grain"),
                dimensions=query.get("dimensions"),
                date_from=query.get("date_from"),
                date_to=query.get("date_to"),
            )["sql"]

        raise GrafanaError(f"Неизвестный вид запроса: {kind!r}")

    # ── компиляция панели ─────────────────────────────────────────────────────
    def _panel(
        self, widget: dict[str, Any], panel_id: int, x: int, y: int, w: int, h: int
    ) -> dict[str, Any]:
        viz = widget.get("viz", "table")
        panel_type = PANEL_TYPES.get(viz)
        if panel_type is None:
            raise GrafanaError(f"Тип визуализации '{viz}' не поддерживается")

        panel: dict[str, Any] = {
            "id": panel_id,
            "title": widget.get("title", ""),
            "type": panel_type,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
        }

        if viz == "text":
            panel["options"] = {
                "mode": "markdown",
                "content": widget.get("content", ""),
            }
            return panel

        panel["datasource"] = {
            "type": "grafana-clickhouse-datasource",
            "uid": self._cfg.datasource_uid,
        }
        panel["targets"] = [
            {
                "refId": "A",
                "datasource": {
                    "type": "grafana-clickhouse-datasource",
                    "uid": self._cfg.datasource_uid,
                },
                "editorType": "sql",
                "rawSql": self._widget_sql(widget),
                "format": 0 if viz in TIME_SERIES_VIZ else 1,
            }
        ]

        field_config: dict[str, Any] = {"defaults": {}, "overrides": []}
        fmt = widget.get("format") or {}
        if fmt.get("unit"):
            field_config["defaults"]["unit"] = fmt["unit"]
        if fmt.get("decimals") is not None:
            field_config["defaults"]["decimals"] = fmt["decimals"]

        if viz == "area":
            field_config["defaults"]["custom"] = {"fillOpacity": 20}
        if viz == "stacked_bar":
            field_config["defaults"]["custom"] = {"stacking": {"mode": "normal"}}

        panel["fieldConfig"] = field_config

        if viz == "stat":
            enc = widget.get("encoding") or {}
            panel["options"] = {
                "reduceOptions": {
                    "calcs": ["lastNotNull"],
                    "fields": f"/^{enc.get('value', '')}$/" if enc.get("value") else "",
                    "values": False,
                },
                "textMode": "auto",
            }

        return panel

    # ── компиляция дашборда ───────────────────────────────────────────────────
    def compile(self, uid: str, title: str, spec: dict[str, Any]) -> dict[str, Any]:
        panels: list[dict[str, Any]] = []
        panel_id = 1
        y = 0

        for row in spec.get("rows", []):
            widgets = row.get("widgets", [])
            x = 0
            row_height = DEFAULT_ROW_HEIGHT
            if widgets and all(w.get("viz") == "stat" for w in widgets):
                row_height = STAT_ROW_HEIGHT

            for widget in widgets:
                # доля из 12 → 24-колоночная сетка Grafana
                width = int(widget.get("width", SPEC_GRID)) * (GRAFANA_GRID // SPEC_GRID)
                width = max(1, min(width, GRAFANA_GRID))
                if x + width > GRAFANA_GRID:
                    x = 0
                    y += row_height
                panels.append(self._panel(widget, panel_id, x, y, width, row_height))
                panel_id += 1
                x += width
            y += row_height

        templating = [self._variable(p) for p in spec.get("params", [])]
        templating = [t for t in templating if t is not None]

        return {
            "uid": uid,
            "title": title,
            "tags": ["mcp-dwh"],
            "timezone": "browser",
            "schemaVersion": 39,
            "refresh": "",
            "time": {"from": "now-90d", "to": "now"},
            "templating": {"list": templating},
            "panels": panels,
        }

    def _variable(self, param: dict[str, Any]) -> dict[str, Any] | None:
        kind = param.get("type")
        name = param.get("name", "")

        if kind == "time_range":
            # период в Grafana — встроенный пикер, отдельной переменной не нужно
            return None

        if kind == "enum":
            options = param.get("options") or {}
            dim = options.get("from_dimension") or {}
            if dim.get("table") and dim.get("column"):
                return {
                    "name": name,
                    "type": "query",
                    "datasource": {
                        "type": "grafana-clickhouse-datasource",
                        "uid": self._cfg.datasource_uid,
                    },
                    "query": f"SELECT DISTINCT {dim['column']} FROM {dim['table']} ORDER BY 1",
                    "multi": bool(param.get("multi")),
                    "includeAll": True,
                    "refresh": 1,
                }
            return {
                "name": name,
                "type": "custom",
                "query": ",".join(str(v) for v in (param.get("values") or [])),
                "multi": bool(param.get("multi")),
                "includeAll": True,
            }

        return {
            "name": name,
            "type": "textbox",
            "query": str(param.get("default", "")),
        }

    # ── публикация ────────────────────────────────────────────────────────────
    def push(self, uid: str, title: str, spec: dict[str, Any]) -> dict[str, Any]:
        """Залить дашборд. Тот же uid перезаписывает — отсюда идемпотентность."""
        dashboard = self.compile(uid, title, spec)
        result = self._request(
            "POST",
            "/api/dashboards/db",
            {
                "dashboard": dashboard,
                "overwrite": True,
                "message": "push из mcp-dwh",
            },
        )
        url = self._cfg.url.rstrip("/") + result.get("url", "")
        return {
            "uid": result.get("uid"),
            "version": result.get("version"),
            "url": url,
            "panels": len(dashboard["panels"]),
            "note": (
                "Push односторонний. Правки внутри интерфейса Grafana будут "
                "затёрты следующим push-ом — меняйте спек, а не панель."
            ),
        }

    def delete(self, uid: str) -> dict[str, Any]:
        self._request("DELETE", f"/api/dashboards/uid/{uid}")
        return {"deleted": uid}
