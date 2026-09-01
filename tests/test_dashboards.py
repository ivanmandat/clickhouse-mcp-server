"""Тесты дашбордов, уровня поддержки и валидации.

Уровень поддержки — не косметика: он определяет, что попадает в фоновый прогон
и в очередь починки. Эти тесты фиксируют именно поведение, а не наличие поля.
"""

from __future__ import annotations

import uuid

import pytest

from mcp_dwh.validation import Validator


@pytest.fixture(scope="module")
def env(ch, sem, dash):
    """Имена метрик и таблиц приходят из фикстур-обнаружения в conftest."""
    return ch, sem, dash, Validator(ch, sem, dash)


@pytest.fixture
def uid(dash):
    generated = "test-" + uuid.uuid4().hex[:10]
    yield generated
    with dash._cursor() as cur:  # noqa: SLF001
        cur.execute("DELETE FROM sem.dashboards WHERE uid = %s", (generated,))


def _metric_spec(metric: str) -> dict:
    return {
        "spec_version": "1",
        "rows": [
            {
                "widgets": [
                    {
                        "id": "w1",
                        "width": 12,
                        "viz": "line",
                        "query": {"kind": "metric", "metric": metric, "grain": "month"},
                        "encoding": {"x": "ts", "y": [metric]},
                    }
                ]
            }
        ],
    }


def _broken_sql_spec(table: str) -> dict:
    return {
        "spec_version": "1",
        "rows": [
            {
                "widgets": [
                    {
                        "id": "w1",
                        "width": 12,
                        "viz": "table",
                        "query": {
                            "kind": "sql",
                            "sql": f"SELECT no_such_column FROM {table}",
                        },
                        "encoding": {"columns": ["no_such_column"]},
                    }
                ]
            }
        ],
    }


# ── поддержка выключена по умолчанию ──────────────────────────────────────────

def test_по_умолчанию_без_поддержки(env, uid, time_metric_name):
    """Разовый отчёт не должен молча становиться обязательством."""
    _, _, dash, _ = env
    saved = dash.save(uid, "Разовый", _metric_spec(time_metric_name))
    assert saved["maintenance"] == "unmaintained"


def test_поддержка_включается_явно(env, uid, time_metric_name):
    _, _, dash, _ = env
    saved = dash.save(uid, "Для команды", _metric_spec(time_metric_name), maintained=True)
    assert saved["maintenance"] == "maintained"


# ── флаг определяет, что попадает в прогон ────────────────────────────────────

def test_прогон_пропускает_неподдерживаемые(env, uid, mart_table):
    _, _, dash, val = env
    dash.save(uid, "Разовый сломанный", _broken_sql_spec(mart_table))
    result = val.validate_all(only_maintained=True)
    assert uid not in [d["uid"] for d in result["broken_dashboards"]]
    assert result["skipped_not_maintained"] >= 1


def test_прогон_по_всем_находит_поломку(env, uid, mart_table):
    _, _, dash, val = env
    dash.save(uid, "Разовый сломанный", _broken_sql_spec(mart_table))
    result = val.validate_all(only_maintained=False)
    assert uid in [d["uid"] for d in result["broken_dashboards"]]


def test_очередь_починки_только_поддерживаемые(env, uid, mart_table):
    _, _, dash, val = env
    dash.save(uid, "Сломанный", _broken_sql_spec(mart_table))
    val.validate_dashboard(uid)
    assert uid not in [b["uid"] for b in dash.broken(only_maintained=True)["broken"]]
    assert uid in [b["uid"] for b in dash.broken(only_maintained=False)["broken"]]


# ── статусы виджетов ──────────────────────────────────────────────────────────

def test_рабочий_дашборд_на_метрике(env, uid, time_metric_name):
    _, _, dash, val = env
    dash.save(uid, "Обзор", _metric_spec(time_metric_name), maintained=True)
    result = val.validate_dashboard(uid)
    assert result["status"] == "ok"


def test_несуществующая_колонка_ломает(env, uid, mart_table):
    _, _, dash, val = env
    dash.save(uid, "Сломанный", _broken_sql_spec(mart_table), maintained=True)
    result = val.validate_dashboard(uid)
    assert result["widgets"][0]["status"] == "broken"


def test_ловит_тихий_дрейф_колонок(env, uid, mart_table, date_column, numeric_column):
    """Запрос выполняется, но вернул не то — график молча пустеет."""
    _, _, dash, val = env
    spec = {
        "spec_version": "1",
        "rows": [
            {
                "widgets": [
                    {
                        "id": "w1",
                        "width": 12,
                        "viz": "line",
                        "query": {
                            "kind": "sql",
                            # запрос вернёт "total", а виджет ждёт "ozhidaemoe"
                            "sql": f"SELECT {date_column}, sum({numeric_column}) AS total "
                            f"FROM {mart_table} GROUP BY {date_column}",
                            "declared_columns": [
                                {"name": date_column, "type": "Date"},
                                {"name": "ozhidaemoe", "type": "Decimal"},
                            ],
                        },
                        "encoding": {"x": date_column, "y": ["ozhidaemoe"]},
                    }
                ]
            }
        ],
    }
    dash.save(uid, "Дрейф", spec, maintained=True)
    result = val.validate_dashboard(uid)
    assert result["widgets"][0]["status"] == "drifted"


def test_пропавшая_метрика(env, uid):
    _, _, dash, val = env
    spec = {
        "spec_version": "1",
        "rows": [
            {
                "widgets": [
                    {
                        "id": "w1",
                        "width": 6,
                        "viz": "stat",
                        "query": {"kind": "metric", "metric": "не_существует"},
                        "encoding": {"value": "x"},
                    }
                ]
            }
        ],
    }
    dash.save(uid, "Призрак", spec, maintained=True)
    result = val.validate_dashboard(uid)
    assert result["widgets"][0]["status"] == "stale_metric"


# ── переходы уровня поддержки ─────────────────────────────────────────────────

def test_снятие_с_поддержки_сбрасывает_broken_since(env, uid, mart_table):
    """Мы за ним больше не следим — значит и «сломан столько-то дней» неуместно."""
    _, _, dash, val = env
    dash.save(uid, "Сломанный", _broken_sql_spec(mart_table), maintained=True)
    val.validate_dashboard(uid)
    assert dash.get(uid)["broken_since"] is not None

    dash.set_maintenance(uid, "unmaintained", reason="Отчёт был разовый")
    record = dash.get(uid)
    assert record["broken_since"] is None
    assert record["maintenance_reason"] == "Отчёт был разовый"


def test_архивный_скрыт_из_выдачи(env, uid, time_metric_name):
    _, _, dash, _ = env
    dash.save(uid, "Устаревший", _metric_spec(time_metric_name))
    dash.set_maintenance(uid, "archived", reason="Проект закрыт")
    assert uid not in [d["uid"] for d in dash.list()["dashboards"]]
    assert uid in [d["uid"] for d in dash.list(include_archived=True)["dashboards"]]


def test_неизвестный_уровень_отклоняется(env, uid, time_metric_name):
    _, _, dash, _ = env
    dash.save(uid, "X", _metric_spec(time_metric_name))
    with pytest.raises(ValueError):
        dash.set_maintenance(uid, "somehow_supported")


def test_снятие_с_поддержки_не_удаляет_спек(env, uid, time_metric_name):
    _, _, dash, _ = env
    dash.save(uid, "X", _metric_spec(time_metric_name), maintained=True)
    dash.set_maintenance(uid, "archived", reason="Больше не нужен")
    assert dash.get(uid)["spec"] is not None


# ── анализ влияния ────────────────────────────────────────────────────────────

def test_влияние_метрики_разделено_по_поддержке(env, uid, time_metric_name):
    _, _, dash, _ = env
    dash.save(uid, "На метрике", _metric_spec(time_metric_name), maintained=True)
    impact = dash.metric_impact(time_metric_name)
    assert uid in [d["uid"] for d in impact["maintained"]]


def test_версии_накапливаются(env, uid, time_metric_name):
    """Повторное сохранение своего дашборда требует явного overwrite."""
    _, _, dash, _ = env
    first = dash.save(uid, "V1", _metric_spec(time_metric_name))
    second = dash.save(uid, "V2", _metric_spec(time_metric_name), overwrite=True)
    assert second["version"] == first["version"] + 1


# ── защита от столкновения имён ───────────────────────────────────────────────
# Сервер общий: два агента, отвечающие на один вопрос, выбирают одно и то же
# естественное имя. Без защиты второй молча затирал дашборд первого.

def test_занятое_имя_не_затирается(env, uid, time_metric_name):
    from mcp_dwh.dashboards import DashboardExists

    _, _, dash, _ = env
    dash.save(uid, "Первый", _metric_spec(time_metric_name), maintained=True)
    with pytest.raises(DashboardExists):
        dash.save(uid, "Второй", _metric_spec(time_metric_name))
    assert dash.get(uid)["title"] == "Первый"


def test_отказ_предлагает_свободное_имя(env, uid, time_metric_name):
    from mcp_dwh.dashboards import DashboardExists

    _, _, dash, _ = env
    dash.save(uid, "Первый", _metric_spec(time_metric_name))
    try:
        dash.save(uid, "Второй", _metric_spec(time_metric_name))
        pytest.fail("ожидался отказ")
    except DashboardExists as exc:
        assert exc.suggestion.startswith(uid)
        assert exc.suggestion != uid
        dash.save(exc.suggestion, "Второй", _metric_spec(time_metric_name))
        assert dash.get(exc.suggestion)["title"] == "Второй"
        with dash._cursor() as cur:  # noqa: SLF001
            cur.execute("DELETE FROM sem.dashboards WHERE uid = %s", (exc.suggestion,))


def test_свой_дашборд_обновляется_явно(env, uid, time_metric_name):
    _, _, dash, _ = env
    dash.save(uid, "Первый", _metric_spec(time_metric_name))
    result = dash.save(uid, "Обновлённый", _metric_spec(time_metric_name), overwrite=True)
    assert result["version"] == 2
    assert dash.get(uid)["title"] == "Обновлённый"
