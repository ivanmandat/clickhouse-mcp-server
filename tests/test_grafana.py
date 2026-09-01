"""Тесты адаптера рендера в Grafana.

Компиляция спека проверяется без сети. Публикация — интеграционно,
пропускается, если Grafana не поднята.
"""

from __future__ import annotations

import uuid

import pytest

from mcp_dwh.grafana import GRAFANA_GRID, GrafanaAdapter, GrafanaError


@pytest.fixture(scope="module")
def adapter(cfg, sem):
    return GrafanaAdapter(cfg.grafana, sem)


@pytest.fixture(scope="module")
def live(adapter):
    try:
        adapter.health()
    except GrafanaError:
        pytest.skip("Grafana недоступна")
    return adapter


def _spec(metric: str, dim_table: str, dim_col: str) -> dict:
    return {
        "spec_version": "1",
        "params": [
            {"name": "period", "type": "time_range", "default": "last_90d"},
            {
                "name": "country",
                "type": "enum",
                "multi": True,
                "options": {
                    "from_dimension": {"table": dim_table, "column": dim_col}
                },
            },
        ],
        "rows": [
            {
                "widgets": [
                    {
                        "id": "a",
                        "width": 4,
                        "viz": "stat",
                        "title": "Выручка",
                        "query": {"kind": "metric", "metric": metric},
                        "encoding": {"value": metric},
                    },
                    {
                        "id": "b",
                        "width": 8,
                        "viz": "line",
                        "title": "Динамика",
                        "query": {
                            "kind": "metric",
                            "metric": metric,
                            "grain": "month",
                        },
                        "encoding": {"x": "ts", "y": [metric]},
                    },
                ]
            }
        ],
    }


# ── компиляция ────────────────────────────────────────────────────────────────

def test_ширина_переводится_в_сетку_grafana(adapter, time_metric_name, mart_table, any_column):
    """Спек считает в 12 долях, у Grafana сетка 24-колоночная."""
    compiled = adapter.compile("t", "T", _spec(time_metric_name, mart_table, any_column))
    widths = [p["gridPos"]["w"] for p in compiled["panels"]]
    assert widths == [8, 16]
    assert sum(widths) == GRAFANA_GRID


def test_панели_не_перекрываются(adapter, time_metric_name, mart_table, any_column):
    compiled = adapter.compile("t", "T", _spec(time_metric_name, mart_table, any_column))
    a, b = compiled["panels"]
    assert a["gridPos"]["x"] + a["gridPos"]["w"] == b["gridPos"]["x"]


def test_виды_визуализации_переводятся(adapter, time_metric_name, mart_table, any_column):
    compiled = adapter.compile("t", "T", _spec(time_metric_name, mart_table, any_column))
    assert [p["type"] for p in compiled["panels"]] == ["stat", "timeseries"]


def test_метрика_разворачивается_в_sql(adapter, sem, time_metric_name, mart_table, any_column):
    """Grafana про метрики не знает — компиляция на стороне сервера."""
    compiled = adapter.compile("t", "T", _spec(time_metric_name, mart_table, any_column))
    definition = sem.get_metric(time_metric_name)
    sql = compiled["panels"][0]["targets"][0]["rawSql"]
    assert definition["sql_expression"] in sql
    assert definition["base_table"] in sql


def test_период_не_становится_переменной(adapter, time_metric_name, mart_table, any_column):
    """time_range — встроенный пикер Grafana, отдельная переменная не нужна."""
    compiled = adapter.compile("t", "T", _spec(time_metric_name, mart_table, any_column))
    names = [v["name"] for v in compiled["templating"]["list"]]
    assert names == ["country"]


def test_неизвестный_тип_визуализации_отклоняется(adapter):
    spec = {"rows": [{"widgets": [{"id": "x", "viz": "sankey", "width": 12}]}]}
    with pytest.raises(GrafanaError):
        adapter.compile("t", "T", spec)


def test_текстовый_виджет_без_запроса(adapter):
    spec = {
        "rows": [
            {"widgets": [{"id": "n", "viz": "text", "width": 12, "content": "# Заметка"}]}
        ]
    }
    panel = adapter.compile("t", "T", spec)["panels"][0]
    assert panel["type"] == "text"
    assert "targets" not in panel


# ── публикация ────────────────────────────────────────────────────────────────

def test_публикация_идемпотентна_по_uid(live, time_metric_name, mart_table, any_column):
    uid = "test-" + uuid.uuid4().hex[:8]
    try:
        first = live.push(uid, "Тест", _spec(time_metric_name, mart_table, any_column))
        second = live.push(uid, "Тест", _spec(time_metric_name, mart_table, any_column))
        assert first["uid"] == second["uid"] == uid
        assert second["version"] > first["version"]
    finally:
        live.delete(uid)


def test_опубликованный_дашборд_отдаёт_ссылку(live, time_metric_name, mart_table, any_column):
    uid = "test-" + uuid.uuid4().hex[:8]
    try:
        result = live.push(uid, "Тест", _spec(time_metric_name, mart_table, any_column))
        assert uid in result["url"]
        assert result["panels"] == 2
    finally:
        live.delete(uid)
