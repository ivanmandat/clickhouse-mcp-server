"""Ветка отчётов: самодостаточный HTML со вшитыми данными.

Отличие от дашборда принципиальное. Дашборд живой: он ходит в ClickHouse при
каждом открытии. Отчёт — срез на момент времени: данные выполняются один раз и
остаются в файле. Поэтому он читается через полгода, когда таблица уже
переименована, и его можно просто отправить в мессенджер.

BI-инструмент здесь не нужен и мешает. Графики рисуются встроенным SVG:
никаких внешних библиотек, файл открывается без сети.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Any

from .clickhouse import ClickHouse
from .semantic import Semantic

PALETTE = [
    "#3D6FA8", "#C77E1A", "#3E8E5A", "#B54545",
    "#7A5FBF", "#2F8F8F", "#B4651E", "#6B7480",
]


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: Any, decimals: int = 0) -> str:
    n = _num(value)
    if n is None:
        return _esc(value)
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:,.1f} млн".replace(",", " ")
    if abs(n) >= 1000:
        return f"{n:,.0f}".replace(",", " ")
    return f"{n:,.{decimals}f}".replace(",", " ")


class ReportRenderer:
    """Собирает HTML-отчёт: выполняет запросы и вшивает результат в файл."""

    def __init__(self, ch: ClickHouse, sem: Semantic) -> None:
        self._ch = ch
        self._sem = sem

    # ── данные виджета ────────────────────────────────────────────────────────
    def _widget_data(self, widget: dict[str, Any]) -> dict[str, Any]:
        query = widget.get("query") or {}
        kind = query.get("kind")

        if kind == "metric":
            sql = self._sem.compile_metric(
                query.get("metric", ""),
                grain=query.get("grain"),
                dimensions=query.get("dimensions"),
                date_from=query.get("date_from"),
                date_to=query.get("date_to"),
            )["sql"]
        elif kind == "sql":
            sql = query.get("sql", "")
        else:
            raise ValueError(f"Неизвестный вид запроса: {kind!r}")

        result = self._ch.query(sql, limit=widget.get("limit", 2000))
        return {"sql": sql, "columns": result.columns, "rows": result.rows}

    # ── отрисовка ─────────────────────────────────────────────────────────────
    def _stat(self, widget: dict[str, Any], data: dict[str, Any]) -> str:
        enc = widget.get("encoding") or {}
        col = enc.get("value")
        value = "—"
        if data["rows"] and col in data["columns"]:
            value = _fmt(data["rows"][0][data["columns"].index(col)])
        unit = (widget.get("format") or {}).get("unit", "")
        return (
            '<div class="stat">'
            f'<div class="stat-label">{_esc(widget.get("title", ""))}</div>'
            f'<div class="stat-value">{value}'
            f'{f"<span class=stat-unit> {_esc(unit)}</span>" if unit else ""}</div>'
            "</div>"
        )

    def _table(self, widget: dict[str, Any], data: dict[str, Any]) -> str:
        cols = (widget.get("encoding") or {}).get("columns") or data["columns"]
        idx = [data["columns"].index(c) for c in cols if c in data["columns"]]
        head = "".join(f"<th>{_esc(data['columns'][i])}</th>" for i in idx)
        body = ""
        for row in data["rows"][:200]:
            cells = "".join(
                f'<td class="{"num" if _num(row[i]) is not None else ""}">'
                f"{_fmt(row[i]) if _num(row[i]) is not None else _esc(row[i])}</td>"
                for i in idx
            )
            body += f"<tr>{cells}</tr>"
        more = ""
        if len(data["rows"]) > 200:
            more = f'<p class="muted">Показаны первые 200 строк из {len(data["rows"])}.</p>'
        return f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>{more}'

    def _chart(self, widget: dict[str, Any], data: dict[str, Any]) -> str:
        """Линейный или столбчатый график встроенным SVG."""
        enc = widget.get("encoding") or {}
        x_col = enc.get("x")
        y_cols = enc.get("y") or []
        series_col = enc.get("series")

        if x_col not in data["columns"] or not y_cols:
            return '<p class="muted">Недостаточно данных для графика.</p>'

        xi = data["columns"].index(x_col)
        yi = data["columns"].index(y_cols[0]) if y_cols[0] in data["columns"] else None
        if yi is None:
            return '<p class="muted">Колонка значения не найдена.</p>'

        # группируем в ряды
        series: dict[str, list[tuple[str, float]]] = {}
        si = data["columns"].index(series_col) if series_col in data["columns"] else None
        for row in data["rows"]:
            value = _num(row[yi])
            if value is None:
                continue
            key = str(row[si]) if si is not None else (y_cols[0])
            series.setdefault(key, []).append((str(row[xi]), value))

        if not series:
            return '<p class="muted">Нет числовых значений.</p>'

        xs: list[str] = []
        for points in series.values():
            for x, _ in points:
                if x not in xs:
                    xs.append(x)
        xs.sort()

        vmax = max(v for pts in series.values() for _, v in pts)
        vmin = min(0.0, min(v for pts in series.values() for _, v in pts))
        span = vmax - vmin or 1.0

        W, H = 860, 260
        PAD_L, PAD_B, PAD_T, PAD_R = 64, 34, 16, 12
        plot_w = W - PAD_L - PAD_R
        plot_h = H - PAD_T - PAD_B

        def px(i: int) -> float:
            if len(xs) == 1:
                return PAD_L + plot_w / 2
            return PAD_L + plot_w * i / (len(xs) - 1)

        def py(v: float) -> float:
            return PAD_T + plot_h * (1 - (v - vmin) / span)

        parts = [
            f'<svg viewBox="0 0 {W} {H}" role="img" '
            f'aria-label="{_esc(widget.get("title", "график"))}">'
        ]

        # сетка и подписи оси значений
        for frac in (0, 0.25, 0.5, 0.75, 1):
            v = vmin + span * frac
            y = py(v)
            parts.append(
                f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}" '
                'class="grid"/>'
            )
            parts.append(
                f'<text x="{PAD_L - 8}" y="{y + 4:.1f}" class="axis" '
                f'text-anchor="end">{_fmt(v)}</text>'
            )

        if widget.get("viz") in ("bar", "stacked_bar"):
            names = list(series)
            group_w = plot_w / max(len(xs), 1) * 0.7
            bar_w = group_w / max(len(names), 1)
            for si_, name in enumerate(names):
                lookup = dict(series[name])
                for i, x in enumerate(xs):
                    v = lookup.get(x)
                    if v is None:
                        continue
                    bx = px(i) - group_w / 2 + si_ * bar_w
                    by = py(v)
                    parts.append(
                        f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w * 0.9:.1f}" '
                        f'height="{py(vmin) - by:.1f}" fill="{PALETTE[si_ % len(PALETTE)]}"/>'
                    )
        else:
            for si_, (name, points) in enumerate(series.items()):
                lookup = dict(points)
                coords = [
                    f"{px(i):.1f},{py(lookup[x]):.1f}"
                    for i, x in enumerate(xs)
                    if x in lookup
                ]
                if coords:
                    parts.append(
                        f'<polyline points="{" ".join(coords)}" fill="none" '
                        f'stroke="{PALETTE[si_ % len(PALETTE)]}" stroke-width="2" '
                        'stroke-linejoin="round"/>'
                    )

        # подписи оси X — не чаще, чем помещается
        step = max(1, len(xs) // 8)
        for i in range(0, len(xs), step):
            parts.append(
                f'<text x="{px(i):.1f}" y="{H - 12}" class="axis" '
                f'text-anchor="middle">{_esc(xs[i][:10])}</text>'
            )
        parts.append("</svg>")

        legend = ""
        if len(series) > 1:
            items = "".join(
                f'<span class="lg"><i style="background:{PALETTE[i % len(PALETTE)]}"></i>'
                f"{_esc(name)}</span>"
                for i, name in enumerate(series)
            )
            legend = f'<div class="legend">{items}</div>'

        return "".join(parts) + legend

    def _widget(self, widget: dict[str, Any]) -> str:
        viz = widget.get("viz", "table")

        if viz == "text":
            return (
                '<section class="block note">'
                f'<p>{_esc(widget.get("content", ""))}</p></section>'
            )

        try:
            data = self._widget_data(widget)
        except Exception as exc:  # noqa: BLE001 — отчёт должен собраться целиком
            return (
                '<section class="block"><h3>'
                f'{_esc(widget.get("title", ""))}</h3>'
                f'<p class="error">Запрос не выполнен: {_esc(str(exc)[:300])}</p>'
                "</section>"
            )

        if viz == "stat":
            return self._stat(widget, data)

        inner = (
            self._table(widget, data)
            if viz == "table"
            else self._chart(widget, data)
        )
        title = widget.get("title", "")
        return (
            '<section class="block">'
            + (f"<h3>{_esc(title)}</h3>" if title else "")
            + inner
            + "</section>"
        )

    # ── сборка ────────────────────────────────────────────────────────────────
    def render(self, title: str, spec: dict[str, Any]) -> str:
        generated = datetime.now(timezone.utc).astimezone()
        body: list[str] = []

        for row in spec.get("rows", []):
            widgets = row.get("widgets", [])
            if widgets and all(w.get("viz") == "stat" for w in widgets):
                tiles = "".join(
                    self._stat(w, self._safe_data(w)) for w in widgets
                )
                body.append(f'<div class="stats">{tiles}</div>')
            else:
                body.extend(self._widget(w) for w in widgets)

        summary = spec.get("summary", "")
        return _TEMPLATE.format(
            title=_esc(title),
            summary=f'<p class="lede">{_esc(summary)}</p>' if summary else "",
            generated=generated.strftime("%d.%m.%Y %H:%M %Z"),
            body="".join(body),
        )

    def _safe_data(self, widget: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._widget_data(widget)
        except Exception:  # noqa: BLE001
            return {"sql": "", "columns": [], "rows": []}


_TEMPLATE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{
  --bg:#F7F8FA; --surface:#fff; --ink:#1C2128; --muted:#5B6472;
  --line:#E7EBF0; --accent:#3D6FA8; --crit:#B54545;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg:#14171C; --surface:#1D2229; --ink:#E8EBF0; --muted:#98A2B0;
    --line:#2A313B; --accent:#6FA3DC; --crit:#D97070;
  }}
}}
* {{ box-sizing:border-box }}
body {{ margin:0; padding:0 20px 64px; background:var(--bg); color:var(--ink);
  font:16px/1.6 "Segoe UI",system-ui,sans-serif }}
.wrap {{ max-width:920px; margin:0 auto }}
header {{ padding:48px 0 8px }}
h1 {{ font-size:clamp(26px,4vw,36px); margin:0 0 10px; letter-spacing:-.02em }}
h3 {{ font-size:16px; margin:0 0 12px; font-weight:600 }}
.lede {{ color:var(--muted); margin:0 0 6px; max-width:640px }}
.meta {{ color:var(--muted); font-size:13px; margin:4px 0 0 }}
.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
  gap:12px; margin:24px 0 }}
.stat {{ background:var(--surface); border:1px solid var(--line);
  border-radius:10px; padding:16px 18px }}
.stat-label {{ color:var(--muted); font-size:13px; margin-bottom:6px }}
.stat-value {{ font-size:28px; font-weight:600; letter-spacing:-.02em }}
.stat-unit {{ font-size:14px; color:var(--muted); font-weight:400 }}
.block {{ background:var(--surface); border:1px solid var(--line);
  border-radius:10px; padding:20px; margin:16px 0; overflow-x:auto }}
.note p {{ margin:0; color:var(--muted) }}
svg {{ display:block; width:100%; height:auto }}
.grid {{ stroke:var(--line); stroke-width:1 }}
.axis {{ fill:var(--muted); font-size:11px }}
.legend {{ display:flex; flex-wrap:wrap; gap:14px; margin-top:12px;
  font-size:13px; color:var(--muted) }}
.lg i {{ display:inline-block; width:10px; height:10px; border-radius:2px;
  margin-right:6px }}
table {{ border-collapse:collapse; width:100%; font-size:14px }}
th {{ text-align:left; font-size:12px; text-transform:uppercase;
  letter-spacing:.05em; color:var(--muted); padding:6px 12px 6px 0;
  border-bottom:1px solid var(--line) }}
td {{ padding:7px 12px 7px 0; border-bottom:1px solid var(--line) }}
td.num {{ text-align:right; font-variant-numeric:tabular-nums }}
.muted {{ color:var(--muted); font-size:13px }}
.error {{ color:var(--crit); font-size:14px }}
footer {{ margin-top:40px; padding-top:16px; border-top:1px solid var(--line);
  color:var(--muted); font-size:13px }}
</style></head><body><div class="wrap">
<header><h1>{title}</h1>{summary}
<p class="meta">Данные на {generated}. Значения зафиксированы в момент
формирования отчёта и не обновляются.</p></header>
{body}
<footer>Сформировано mcp-dwh. Файл самодостаточный: открывается без сети.</footer>
</div></body></html>"""
