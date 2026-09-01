"""MCP-сервер: инструменты для построения дашбордов и отчётов.

Схема ClickHouse НЕ вкладывается в промпт — агент добывает её вызовами
интроспекции. Поэтому изменение структуры базы не требует правок сервера.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import MCPServer

from .clickhouse import ClickHouse, QueryRejected
from .config import load_config
from .dashboards import DashboardExists, Dashboards
from .db import make_pool
from .grafana import GrafanaAdapter, GrafanaError
from .introspect import describe_table, profile_column, sample_data, search_tables
from .reports import ReportRenderer
from .schema_watch import SchemaWatcher
from .semantic import Semantic
from .validation import Validator

_INSTRUCTIONS = """\
Хранилище на ClickHouse из четырёх слоёв: raw → int → core → mart.
Доступны только два верхних, остальные закрыты грантами.

Порядок работы:

1. ch_search_tables — найдите подходящие таблицы. Полного списка нет и не нужно.
   Для дашбордов по умолчанию берите слой mart: джойны там уже сделаны.
2. sem_get_table_info — что таблица значит, какова её гранулярность и оговорки.
   ch_describe_table — физическая правда: типы, комментарии, ключ сортировки.
3. sem_search_metrics — ПЕРЕД тем как писать агрегат руками. Виджет на метрике
   переживает переименование колонки без починки, на сыром SQL — нет.
4. ch_profile_column — перед фильтрацией по строковому полю. Набор значений и
   регистр в данных часто не такие, как подсказывает название.
5. ch_validate_query — перед тем как сохранить запрос в дашборд.

Если выяснили о данных что-то, чего нет в семантическом слое, — запишите это
через sem_suggest_update. Слой так пополняется по ходу обычной работы.
"""

mcp = MCPServer("mcp-dwh", instructions=_INSTRUCTIONS)

_cfg = load_config()
_ch = ClickHouse(_cfg.clickhouse)
_pg_pool = make_pool(_cfg.postgres)
_sem = Semantic(_cfg.postgres, pool=_pg_pool)
_dash = Dashboards(_cfg.postgres, pool=_pg_pool)
_validator = Validator(_ch, _sem, _dash)
_grafana = GrafanaAdapter(_cfg.grafana, _sem)
_reports = ReportRenderer(_ch, _sem)
_watcher = SchemaWatcher(_ch, _sem)

# Из конфигурации: расчёт по __file__ в контейнере давал каталог
# интерпретатора, и отчёты уходили мимо смонтированного тома.
REPORTS_DIR = _cfg.reports_dir


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


# ══ интроспекция схемы ════════════════════════════════════════════════════════

@mcp.tool()
def ch_search_tables(query: str = "", limit: int = 40) -> str:
    """Найти таблицы по имени, комментарию или именам колонок.

    Начинайте отсюда: полного списка таблиц у вас нет и не нужно.
    Видны только слои core и mart; для дашбордов по умолчанию берите mart.
    """
    return _dump(search_tables(_ch, query, limit))


@mcp.tool()
def ch_describe_table(table: str) -> str:
    """Колонки, типы, комментарии, ключ сортировки и партиционирования.

    Ключ сортировки смотрите обязательно: запрос, попадающий в него,
    на порядок дешевле. Аргумент — имя вида 'mart.obt_sales'.
    """
    return _dump(describe_table(_ch, table))


@mcp.tool()
def ch_sample_data(table: str, n: int = 5) -> str:
    """Несколько строк таблицы: показывает реальные форматы значений."""
    return _dump(sample_data(_ch, table, n))


@mcp.tool()
def ch_profile_column(table: str, column: str, top: int = 15) -> str:
    """Кардинальность, доля пустых и top-N значений колонки.

    Вызывайте перед тем, как фильтровать по строковому полю: регистр и набор
    значений в данных часто не такие, как кажется.
    """
    return _dump(profile_column(_ch, table, column, top))


# ══ выполнение запросов ═══════════════════════════════════════════════════════

@mcp.tool()
def ch_run_query(sql: str, limit: int = 200) -> str:
    """Выполнить SELECT. Только чтение, лимиты и таймауты заданы на сервере.

    Если LIMIT в запросе не указан, он будет добавлен автоматически.
    """
    try:
        return _dump(_ch.query(sql, limit).to_dict())
    except QueryRejected as exc:
        return _dump({"error": str(exc), "rejected_before_execution": True})
    except Exception as exc:  # noqa: BLE001 — текст ошибки нужен агенту для починки
        return _dump({"error": str(exc)})


@mcp.tool()
def ch_validate_query(sql: str) -> str:
    """Проверить запрос, не выполняя его: синтаксис и набор возвращаемых колонок.

    Дёшево. Используйте перед сохранением запроса в дашборд.
    """
    return _dump(_ch.validate(sql))


# ══ семантический слой ════════════════════════════════════════════════════════

@mcp.tool()
def sem_search_metrics(query: str = "", limit: int = 30) -> str:
    """Найти готовую метрику. Вызывайте ДО того, как писать агрегат руками.

    Виджет на метрике переживает переименование колонки без починки:
    правится одно определение, и все виджеты следуют за ним.
    """
    return _dump(_sem.search_metrics(query, limit))


@mcp.tool()
def sem_get_metric(name: str) -> str:
    """Полное определение метрики: выражение, базовая таблица, измерения."""
    metric = _sem.get_metric(name)
    if metric is None:
        return _dump({"error": f"Метрика '{name}' не найдена"})
    return _dump(metric)


@mcp.tool()
def sem_compile_metric(
    name: str,
    grain: str = "",
    dimensions: list[str] | None = None,
    date_from: str = "",
    date_to: str = "",
) -> str:
    """Развернуть метрику в готовый SQL.

    grain: day | week | month | quarter | year (пусто — без разбивки по времени).
    dimensions: измерения для группировки, например ["customer_country"].
    """
    try:
        return _dump(
            _sem.compile_metric(
                name,
                grain=grain or None,
                dimensions=dimensions,
                date_from=date_from or None,
                date_to=date_to or None,
            )
        )
    except ValueError as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def sem_get_table_info(table: str) -> str:
    """Смысл таблицы: слой, гранулярность, оговорки, подсказки по значениям.

    Дополняет ch_describe_table: там физическая правда, здесь — что она значит.
    """
    return _dump(_sem.get_table_semantics(table))


@mcp.tool()
def sem_get_relations(table: str = "") -> str:
    """Как джойнить таблицы. В ClickHouse внешних ключей нет — только отсюда."""
    return _dump(_sem.get_relations(table or None))


@mcp.tool()
def sem_suggest_update(
    kind: str,
    target: str,
    description: str = "",
    column: str = "",
    value_hints: dict | None = None,
) -> str:
    """Дописать в семантический слой то, что вы выяснили о данных.

    Вызывайте, когда обнаружили расхождение: неописанное значение статуса,
    неочевидную оговорку, реальную гранулярность. Слой так пополняется как
    побочный эффект обычной работы. Записи, проверенные человеком, не трогаются.

    kind: 'table' или 'column'. Для 'column' обязателен параметр column.
    """
    try:
        return _dump(
            _sem.suggest_update(
                kind=kind,
                target=target,
                description=description or None,
                column=column or None,
                value_hints=value_hints,
            )
        )
    except ValueError as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def sem_review_queue(limit: int = 30) -> str:
    """Что в семантическом слое ждёт проверки человеком: черновики и устаревшее."""
    return _dump(_sem.review_queue(limit))


# ══ дашборды ══════════════════════════════════════════════════════════════════

@mcp.tool()
def dash_save(
    uid: str,
    title: str,
    spec: dict,
    maintained: bool = False,
    note: str = "",
    overwrite: bool = False,
) -> str:
    """Сохранить дашборд.

    maintained определяет, берёт ли система его на сопровождение:

      maintained=True  — дашборд для команды. Валидируется при изменениях схемы,
                         поломка попадает в очередь починки.
      maintained=False — разовый ответ на вопрос (по умолчанию). Хранится, но
                         не проверяется и не шумит, когда схема уедет.

    Ставьте True только если дашборд действительно нужен надолго: поддержка
    каждого стоит времени фонового прогона.

    overwrite=False защищает от столкновения имён: сервер общий, и занятый uid,
    скорее всего, принадлежит чужому дашборду. В ответе придёт свободное имя.
    Передавайте overwrite=True, только когда обновляете СВОЙ дашборд.
    """
    try:
        return _dump(
            _dash.save(uid, title, spec, maintained=maintained,
                       note=note or None, overwrite=overwrite)
        )
    except DashboardExists as exc:
        return _dump({
            "error": str(exc),
            "uid_taken": exc.uid,
            "suggested_uid": exc.suggestion,
        })
    except Exception as exc:  # noqa: BLE001
        return _dump({"error": str(exc)})


@mcp.tool()
def dash_get(uid: str) -> str:
    """Полный спек дашборда и его текущее состояние."""
    record = _dash.get(uid)
    if record is None:
        return _dump({"error": f"Дашборд '{uid}' не найден"})
    return _dump(record)


@mcp.tool()
def dash_list(maintenance: str = "", include_archived: bool = False, limit: int = 50) -> str:
    """Список дашбордов. Архивные по умолчанию скрыты.

    maintenance: пусто — все кроме архивных; либо maintained / unmaintained / archived.
    """
    try:
        return _dump(
            _dash.list(
                maintenance=maintenance or None,
                include_archived=include_archived,
                limit=limit,
            )
        )
    except ValueError as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def dash_set_maintenance(uid: str, level: str, reason: str = "") -> str:
    """Изменить уровень поддержки дашборда.

      maintained    валидируется, поломка попадает в очередь починки
      unmaintained  хранится, но не проверяется — для разовых и устаревших
      archived      скрыт из выдачи; данные не удаляются

    Снятие с поддержки — не удаление: спек сохраняется, дашборд можно вернуть.
    Указывайте reason: через полгода никто не вспомнит, почему его отключили.
    """
    try:
        return _dump(_dash.set_maintenance(uid, level, reason or None))
    except ValueError as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def dash_validate(uid: str) -> str:
    """Проверить дашборд против актуальной схемы: каждый виджет отдельно.

    Статусы виджета: ok, broken (запрос не компилируется), drifted (выполняется,
    но вернул не те колонки — график молча пустеет), stale_metric.
    """
    try:
        return _dump(_validator.validate_dashboard(uid))
    except ValueError as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def dash_list_broken(only_maintained: bool = True) -> str:
    """Сломанные дашборды с текстом ошибки по каждому виджету.

    По умолчанию только те, что на поддержке: остальные сломаны легально.
    Это и есть вход для починки — ошибка плюс актуальная схема.
    """
    return _dump(_dash.broken(only_maintained=only_maintained))


@mcp.tool()
def dash_validate_all(only_maintained: bool = True) -> str:
    """Прогнать проверку по всем дашбордам на поддержке."""
    return _dump(_validator.validate_all(only_maintained=only_maintained))


@mcp.tool()
def dash_unmaintain_candidates(days: int = 30) -> str:
    """Кого предложить снять с поддержки: сломаны давно и никем не починены.

    Возвращает предложение, а не выполненное действие. Решение о том, что отчёт
    больше не нужен, принимает человек — спросите владельца перед снятием.
    """
    return _dump(_dash.unmaintain_candidates(days))


@mcp.tool()
def dash_metric_impact(metric: str) -> str:
    """Какие дашборды сломает изменение метрики, с разбивкой по поддержке.

    Вызывайте ПЕРЕД правкой определения метрики.
    """
    return _dump(_dash.metric_impact(metric))


# ══ публикация в Grafana ══════════════════════════════════════════════════════

@mcp.tool()
def dash_publish(uid: str) -> str:
    """Опубликовать сохранённый дашборд в Grafana и вернуть ссылку.

    Push односторонний и идемпотентный: тот же uid перезаписывает дашборд.
    Правки, сделанные внутри интерфейса Grafana, будут затёрты следующей
    публикацией — менять нужно спек через dash_save, а не панель руками.

    Метрики разворачиваются в SQL здесь: Grafana про них ничего не знает.
    """
    record = _dash.get(uid)
    if record is None:
        return _dump({"error": f"Дашборд '{uid}' не найден"})

    spec = record["spec"]
    if isinstance(spec, str):
        spec = json.loads(spec)

    try:
        return _dump(_grafana.push(uid, record["title"], spec))
    except GrafanaError as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def dash_unpublish(uid: str) -> str:
    """Убрать дашборд из Grafana. Спек в Postgres при этом сохраняется."""
    try:
        return _dump(_grafana.delete(uid))
    except GrafanaError as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def dash_preview_grafana(uid: str) -> str:
    """Показать, во что скомпилируется спек, без публикации.

    Полезно, чтобы проверить раскладку и итоговый SQL панелей.
    """
    record = _dash.get(uid)
    if record is None:
        return _dump({"error": f"Дашборд '{uid}' не найден"})
    spec = record["spec"]
    if isinstance(spec, str):
        spec = json.loads(spec)
    try:
        compiled = _grafana.compile(uid, record["title"], spec)
    except GrafanaError as exc:
        return _dump({"error": str(exc)})
    return _dump(
        {
            "uid": uid,
            "panels": [
                {
                    "title": p.get("title"),
                    "type": p.get("type"),
                    "gridPos": p.get("gridPos"),
                    "sql": (p.get("targets") or [{}])[0].get("rawSql"),
                }
                for p in compiled["panels"]
            ],
            "variables": [v["name"] for v in compiled["templating"]["list"]],
        }
    )


# ══ отчёты ════════════════════════════════════════════════════════════════════

@mcp.tool()
def report_render(title: str, spec: dict, filename: str = "") -> str:
    """Собрать самодостаточный HTML-отчёт и сохранить в файл.

    Отличие от дашборда принципиальное: дашборд живой и перезапрашивает данные
    при каждом открытии, отчёт — срез на момент времени. Запросы выполняются
    один раз, значения вшиваются в файл. Такой файл читается через полгода,
    когда таблица уже переименована, и его можно просто отправить.

    Формат спека тот же, что у дашборда: rows -> widgets. Дополнительно
    поддерживается поле summary с текстом вводки.
    """
    import re
    from pathlib import Path

    safe = re.sub(r"[^\w\-]+", "-", filename or title).strip("-").lower() or "report"
    path = REPORTS_DIR / f"{safe}.html"
    try:
        html = _reports.render(title, spec)
    except Exception as exc:  # noqa: BLE001
        return _dump({"error": str(exc)})

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8", newline="\n")
    return _dump(
        {
            "path": str(path),
            "size_bytes": len(html.encode("utf-8")),
            "external_requests": 0,
            "note": "Файл самодостаточный: графики нарисованы встроенным SVG, "
            "данные зафиксированы на момент формирования.",
        }
    )


# ══ схема: снимки, дифф, реакция ══════════════════════════════════════════════

@mcp.tool()
def schema_snapshot() -> str:
    """Снять срез физической схемы и сохранить его.

    Фундамент механизма обновления: дифф двух снимков порождает черновики
    для нового и статус orphaned для пропавшего.
    """
    return _dump(_watcher.take_snapshot())


@mcp.tool()
def schema_diff(from_snapshot: int = 0, to_snapshot: int = 0) -> str:
    """Что изменилось между снимками. По умолчанию — два последних.

    Событие column_dropped может нести rename_candidates: колонки того же типа,
    появившиеся в том же диффе. Однозначное переименование помечается как
    column_renamed?, неоднозначное остаётся удалением со списком кандидатов.
    """
    return _dump(
        _watcher.diff(from_snapshot or None, to_snapshot or None)
    )


@mcp.tool()
def schema_apply_changes(from_snapshot: int = 0, to_snapshot: int = 0) -> str:
    """Привести семантический слой в соответствие с диффом.

    Новое получает черновики, пропавшее помечается orphaned, метрики, которые
    на него опирались, — stale. Ничего не удаляется: записи orphaned и есть
    список того, что сломалось.

    После вызова смотрите sem_review_queue и dash_list_broken.
    """
    return _dump(
        _watcher.apply(from_snapshot or None, to_snapshot or None)
    )


# ══ диагностика ═══════════════════════════════════════════════════════════════

@mcp.tool()
def mcp_diagnostics() -> str:
    """Проверить связь с ClickHouse, Postgres и Grafana; показать настройки.

    Вызывайте первым, если инструменты возвращают ошибки подключения: покажет,
    к каким адресам сервер на самом деле обращается. Пароли скрыты.
    """
    checks: dict[str, Any] = {}

    try:
        res = _ch.query("SELECT version()")
        checks["clickhouse"] = {
            "ok": True,
            "version": res.rows[0][0],
            "pool": _ch.pool_stats,
        }
    except Exception as exc:  # noqa: BLE001
        checks["clickhouse"] = {"ok": False, "error": str(exc)[:300]}

    try:
        rows = _sem._fetch("SELECT version() AS v")  # noqa: SLF001
        checks["postgres"] = {
            "ok": True,
            "version": rows[0]["v"].split(",")[0],
            "metrics": len(_sem.search_metrics("", 200)["metrics"]),
            "dashboards": _dash.list(include_archived=True, limit=1000)["count"],
        }
    except Exception as exc:  # noqa: BLE001
        checks["postgres"] = {"ok": False, "error": str(exc)[:300]}

    try:
        checks["grafana"] = {"ok": True, **_grafana.health()}
    except Exception as exc:  # noqa: BLE001
        # Grafana нужна только для dash_publish, её недоступность не критична
        checks["grafana"] = {"ok": False, "error": str(exc)[:200], "required": False}

    writable = False
    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        probe = REPORTS_DIR / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        writable = True
    except Exception:  # noqa: BLE001
        pass
    checks["reports_dir"] = {"path": str(REPORTS_DIR), "writable": writable}

    return _dump({"checks": checks, "config": _cfg.redacted()})


def main() -> None:
    """Точка входа.

    MCP_TRANSPORT=stdio (по умолчанию) — клиент запускает процесс сам.
    MCP_TRANSPORT=http — сервер на стороне заказчика, общий для пользователей;
    в этом режиме обязателен MCP_AUTH_TOKEN.
    """
    import os

    transport = os.getenv("MCP_TRANSPORT", "stdio").lower()
    try:
        if transport in ("http", "streamable-http"):
            from . import __version__
            from .http_server import serve

            serve(mcp, __version__)
        else:
            mcp.run()
    finally:
        _ch.close()
        _sem.close()
        _dash.close()
        _pg_pool.close()


if __name__ == "__main__":
    main()
