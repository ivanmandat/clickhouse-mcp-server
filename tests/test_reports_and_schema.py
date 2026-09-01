"""Тесты ветки отчётов и механизма отслеживания схемы."""

from __future__ import annotations

import pytest

from mcp_dwh.reports import ReportRenderer
from mcp_dwh.schema_watch import SchemaWatcher
from mcp_dwh.semantic import Semantic


@pytest.fixture(scope="module")
def env(ch, sem):
    return ch, sem


@pytest.fixture
def renderer(ch, sem):
    return ReportRenderer(ch, sem)


def _spec(metric: str) -> dict:
    return {
        "summary": "Тестовый отчёт",
        "rows": [
            {
                "widgets": [
                    {
                        "id": "s",
                        "viz": "stat",
                        "title": "Выручка",
                        "query": {"kind": "metric", "metric": metric},
                        "encoding": {"value": metric},
                    }
                ]
            },
            {
                "widgets": [
                    {
                        "id": "t",
                        "viz": "line",
                        "title": "Динамика",
                        "query": {
                            "kind": "metric",
                            "metric": metric,
                            "grain": "month",
                        },
                        "encoding": {"x": "ts", "y": [metric]},
                    }
                ]
            },
        ],
    }


# ── отчёты ────────────────────────────────────────────────────────────────────

def test_отчёт_самодостаточен(renderer, time_metric_name):
    """Ни одной внешней ссылки: файл должен открываться без сети."""
    html = renderer.render("Тест", _spec(time_metric_name))
    assert "http://" not in html
    assert "https://" not in html
    assert "<script" not in html


def test_график_рисуется_встроенным_svg(renderer, time_metric_name):
    html = renderer.render("Тест", _spec(time_metric_name))
    assert "<svg" in html
    assert "<polyline" in html


def test_данные_вшиты_в_файл(renderer, time_metric_name):
    """Значения зафиксированы: в файле есть цифры, а не запрос за ними."""
    html = renderer.render("Тест", _spec(time_metric_name))
    assert "stat-value" in html
    assert "не обновляются" in html


def test_ошибка_виджета_не_рушит_отчёт(renderer, mart_table):
    """Отчёт должен собраться целиком, показав проблему в своём блоке."""
    spec = {
        "rows": [
            {
                "widgets": [
                    {
                        "id": "bad",
                        "viz": "table",
                        "title": "Сломанный",
                        "query": {"kind": "sql", "sql": f"SELECT nope FROM {mart_table}"},
                    },
                    {
                        "id": "good",
                        "viz": "table",
                        "title": "Рабочий",
                        "query": {
                            "kind": "sql",
                            "sql": f"SELECT * FROM {mart_table} LIMIT 3",
                        },
                    },
                ]
            }
        ]
    }
    html = renderer.render("Тест", spec)
    assert 'class="error"' in html
    assert "Рабочий" in html


def test_текстовый_блок_без_запроса(renderer):
    spec = {"rows": [{"widgets": [{"id": "n", "viz": "text", "content": "Примечание"}]}]}
    assert "Примечание" in renderer.render("Тест", spec)


# ── дифф схемы ────────────────────────────────────────────────────────────────

def test_снимок_пишется(env):
    ch, sem = env
    w = SchemaWatcher(ch, sem)
    snap = w.take_snapshot()
    try:
        assert snap["tables"] > 0
        assert snap["columns"] > snap["tables"]
    finally:
        with sem._cursor() as cur:  # noqa: SLF001
            cur.execute(
                "DELETE FROM sem.schema_snapshots WHERE id = %s", (snap["snapshot_id"],)
            )


def test_одинаковые_снимки_дают_пустой_дифф(env):
    ch, sem = env
    w = SchemaWatcher(ch, sem)
    a = w.take_snapshot()
    b = w.take_snapshot()
    try:
        assert w.diff(a["snapshot_id"], b["snapshot_id"])["events"] == []
    finally:
        with sem._cursor() as cur:  # noqa: SLF001
            cur.execute(
                "DELETE FROM sem.schema_snapshots WHERE id = ANY(%s)",
                ([a["snapshot_id"], b["snapshot_id"]],),
            )


def _fake_diff(sem, old: dict, new: dict) -> list[dict]:
    """Кладём два синтетических снимка и сравниваем их."""
    import json

    ids = []
    with sem._cursor() as cur:  # noqa: SLF001
        for snap in (old, new):
            cur.execute(
                "INSERT INTO sem.schema_snapshots (snapshot) VALUES (%s::jsonb) "
                "RETURNING id",
                (json.dumps(snap),),
            )
            ids.append(cur.fetchone()["id"])
    return ids


def test_однозначное_переименование_распознаётся(env):
    ch, sem = env
    w = SchemaWatcher(ch, sem)
    old = {"mart.t": {"a": {"type": "UInt64", "comment": ""}}}
    new = {"mart.t": {"b": {"type": "UInt64", "comment": ""}}}
    ids = _fake_diff(sem, old, new)
    try:
        events = w.diff(ids[0], ids[1])["events"]
        assert events[0]["kind"] == "column_renamed?"
        assert (events[0]["from"], events[0]["to"]) == ("a", "b")
    finally:
        with sem._cursor() as cur:  # noqa: SLF001
            cur.execute("DELETE FROM sem.schema_snapshots WHERE id = ANY(%s)", (ids,))


def test_неоднозначное_переименование_отдаёт_кандидатов(env):
    """Две новые колонки того же типа — угадывать нельзя, но список полезен."""
    ch, sem = env
    w = SchemaWatcher(ch, sem)
    old = {"mart.t": {"a": {"type": "UInt64", "comment": ""}}}
    new = {
        "mart.t": {
            "b": {"type": "UInt64", "comment": ""},
            "c": {"type": "UInt64", "comment": ""},
        }
    }
    ids = _fake_diff(sem, old, new)
    try:
        events = w.diff(ids[0], ids[1])["events"]
        dropped = [e for e in events if e["kind"] == "column_dropped"]
        assert dropped and sorted(dropped[0]["rename_candidates"]) == ["b", "c"]
        assert not [e for e in events if e["kind"] == "column_renamed?"]
    finally:
        with sem._cursor() as cur:  # noqa: SLF001
            cur.execute("DELETE FROM sem.schema_snapshots WHERE id = ANY(%s)", (ids,))


def test_смена_типа_колонки(env):
    ch, sem = env
    w = SchemaWatcher(ch, sem)
    old = {"mart.t": {"a": {"type": "UInt32", "comment": ""}}}
    new = {"mart.t": {"a": {"type": "UInt64", "comment": ""}}}
    ids = _fake_diff(sem, old, new)
    try:
        events = w.diff(ids[0], ids[1])["events"]
        assert events[0]["kind"] == "column_type_changed"
        assert events[0]["to"] == "UInt64"
    finally:
        with sem._cursor() as cur:  # noqa: SLF001
            cur.execute("DELETE FROM sem.schema_snapshots WHERE id = ANY(%s)", (ids,))


def test_добавление_и_удаление_таблицы(env):
    ch, sem = env
    w = SchemaWatcher(ch, sem)
    old = {"mart.gone": {"a": {"type": "UInt64", "comment": ""}}}
    new = {"mart.fresh": {"a": {"type": "UInt64", "comment": ""}}}
    ids = _fake_diff(sem, old, new)
    try:
        kinds = {e["kind"] for e in w.diff(ids[0], ids[1])["events"]}
        assert kinds == {"table_added", "table_dropped"}
    finally:
        with sem._cursor() as cur:  # noqa: SLF001
            cur.execute("DELETE FROM sem.schema_snapshots WHERE id = ANY(%s)", (ids,))
