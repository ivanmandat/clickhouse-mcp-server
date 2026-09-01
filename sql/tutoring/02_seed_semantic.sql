-- Семантический слой для домена репетиторства.
--
-- Старые записи не удаляются: метрики домена продаж переводятся в orphaned,
-- чтобы в истории осталось видно, что они были и почему исчезли.

UPDATE sem.metrics
   SET status = 'orphaned', updated_at = now()
 WHERE base_table LIKE '%obt_sales%' OR base_table LIKE '%daily_sales%';

-- ── описания таблиц ───────────────────────────────────────────────────────────
INSERT INTO sem.tables (database_name, table_name, layer, description, usage_notes, grain, origin, status)
VALUES
    ('mart', 'obt_lessons', 'mart',
     'Единая таблица занятий: измерения ученика, репетитора и самого занятия расплющены в одну строку.',
     'Основная таблица для дашбордов. Выручкой считается поле revenue — оно уже равно нулю для несостоявшихся и неоплаченных занятий, поэтому суммировать можно без фильтра. Поле price этим свойством НЕ обладает.',
     'одна строка = одно занятие',
     'human', 'verified'),

    ('mart', 'daily_lessons', 'mart',
     'Предагрегат занятий по дню, предмету, формату и квалификации репетитора.',
     'Берите для трендов и длинных периодов, если разрез укладывается в четыре измерения.',
     'день × предмет × формат × квалификация', 'human', 'verified'),

    ('mart', 'student_progress', 'mart',
     'Витрина ученика: посещаемость, домашние работы, экзамены и выручка.',
     'Гранулярность — один ученик, поэтому суммировать по времени нельзя. Для динамики берите obt_lessons.',
     'один ученик', 'human', 'verified'),

    ('mart', 'tutor_performance', 'mart',
     'Витрина репетитора: нагрузка, выручка и результаты его учеников.',
     'exam_pass_rate и avg_student_homework_score считаются по всем ученикам репетитора и могут быть NULL, если учеников с экзаменами не было.',
     'один репетитор', 'human', 'verified'),

    ('mart', 'exam_outcomes', 'mart',
     'Итоги обучения: сдача экзаменов по предметам и типам.',
     'lessons_before_exam_avg показывает связь между числом занятий и результатом — основной аргумент в отчётах для родителей.',
     'день × предмет × тип экзамена × класс', 'human', 'verified'),

    ('core', 'fct_lesson', 'core',
     'Факт занятия без расплющенных измерений.',
     'Для дашбордов предпочитайте mart.obt_lessons.', 'одно занятие', 'human', 'verified'),
    ('core', 'fct_homework', 'core',
     'Факт домашней работы, привязан к занятию.',
     'score_pct равен NULL для непроверенных работ — учитывайте при усреднении.',
     'одна домашняя работа', 'human', 'verified'),
    ('core', 'fct_exam', 'core',
     'Факт экзамена. Итог обучения.',
     'У ученика может быть несколько попыток: смотрите attempt_no.',
     'один экзамен', 'human', 'verified'),
    ('core', 'dim_student', 'core',
     'Измерение: ученики. Тестовые аккаунты исключены.',
     'grade равен NULL для взрослых учеников.', 'один ученик', 'human', 'verified'),
    ('core', 'dim_tutor', 'core',
     'Измерение: репетиторы.', NULL, 'один репетитор', 'human', 'verified')
ON CONFLICT (database_name, table_name) DO UPDATE
   SET layer = EXCLUDED.layer,
       description = EXCLUDED.description,
       usage_notes = EXCLUDED.usage_notes,
       grain = EXCLUDED.grain,
       origin = 'human', status = 'verified', updated_at = now();

-- ── подсказки по значениям ────────────────────────────────────────────────────
INSERT INTO sem.columns (table_id, column_name, description, value_hints, origin, status)
SELECT t.id, v.col, v.descr, v.hints::jsonb, 'human', 'verified'
FROM sem.tables t
JOIN (VALUES
    ('mart', 'obt_lessons', 'status', 'Статус занятия.',
     '{"values": ["completed", "cancelled", "no_show", "rescheduled"], "case": "нижний регистр", "note": "no_show — ученик не пришёл без предупреждения, cancelled — отмена заранее. Для посещаемости важна разница."}'),
    ('mart', 'obt_lessons', 'revenue', 'Выручка с занятия.',
     '{"note": "Уже равна нулю, если занятие не состоялось или не оплачено. Суммировать можно без фильтра — в отличие от price."}'),
    ('mart', 'obt_lessons', 'price', 'Стоимость занятия по прайсу.',
     '{"note": "НЕ выручка: заполнена и для отменённых занятий. Для денег берите revenue."}'),
    ('mart', 'obt_lessons', 'payment_status', 'Статус оплаты.',
     '{"values": ["paid", "pending", "refunded"]}'),
    ('mart', 'obt_lessons', 'format', 'Формат занятия.',
     '{"values": ["online", "offline"], "note": "Около трёх четвертей занятий проходят онлайн."}'),
    ('mart', 'obt_lessons', 'subject', 'Предмет занятия.',
     '{"values": ["Математика", "Русский язык", "Английский", "Физика", "Химия", "Биология", "Информатика", "Обществознание"]}'),
    ('mart', 'obt_lessons', 'student_grade', 'Класс ученика.',
     '{"note": "NULL для взрослых учеников — при группировке они выпадут, если не обработать явно."}'),
    ('core', 'fct_exam', 'is_passed', 'Признак сдачи экзамена.',
     '{"note": "Считается как score >= passing_score. Порог сдачи 40 из 100."}'),
    ('core', 'fct_homework', 'score_pct', 'Процент за домашнюю работу.',
     '{"note": "NULL для несданных и непроверенных работ. avg() их игнорирует, count() — нет."}')
) AS v(db, tbl, col, descr, hints)
  ON t.database_name = v.db AND t.table_name = v.tbl
ON CONFLICT (table_id, column_name) DO UPDATE
   SET description = EXCLUDED.description, value_hints = EXCLUDED.value_hints,
       origin = 'human', status = 'verified', updated_at = now();

-- ── метрики домена ────────────────────────────────────────────────────────────
INSERT INTO sem.metrics
    (name, display_name, description, sql_expression, base_table, time_column,
     dimensions, filters, unit, origin, status)
VALUES
    ('lesson_revenue', 'Выручка с занятий',
     'Сумма оплаченных состоявшихся занятий. Поле revenue уже обнулено для остальных, поэтому фильтр не нужен.',
     'sum(revenue)', 'mart.obt_lessons', 'lesson_date',
     '["subject", "format", "tutor_qualification", "student_city"]'::jsonb,
     NULL, 'RUB', 'human', 'verified'),

    ('lessons_completed', 'Состоявшиеся занятия',
     'Число проведённых занятий.',
     'countIf(is_completed = 1)', 'mart.obt_lessons', 'lesson_date',
     '["subject", "format", "tutor_qualification", "student_city"]'::jsonb,
     NULL, 'шт', 'human', 'verified'),

    ('attendance_rate', 'Посещаемость',
     'Доля состоявшихся занятий от запланированных.',
     'countIf(is_completed = 1) / count()', 'mart.obt_lessons', 'lesson_date',
     '["subject", "format", "tutor_qualification"]'::jsonb,
     NULL, 'доля', 'human', 'verified'),

    ('no_show_rate', 'Доля неявок',
     'Доля занятий, на которые ученик не пришёл без предупреждения. Отмены заранее сюда не входят.',
     'countIf(is_no_show = 1) / count()', 'mart.obt_lessons', 'lesson_date',
     '["subject", "format", "tutor_qualification"]'::jsonb,
     NULL, 'доля', 'human', 'verified'),

    ('active_students', 'Учеников с занятиями',
     'Число уникальных учеников, у которых было занятие в периоде.',
     'uniqExact(student_id)', 'mart.obt_lessons', 'lesson_date',
     '["subject", "format", "student_city"]'::jsonb,
     NULL, 'чел', 'human', 'verified'),

    ('active_tutors', 'Работающих репетиторов',
     'Число уникальных репетиторов, проводивших занятия в периоде.',
     'uniqExact(tutor_id)', 'mart.obt_lessons', 'lesson_date',
     '["subject", "format"]'::jsonb,
     NULL, 'чел', 'human', 'verified'),

    ('avg_lesson_price', 'Средняя стоимость занятия',
     'Средний чек состоявшегося оплаченного занятия.',
     'sum(revenue) / countIf(is_completed = 1 AND is_paid = 1)',
     'mart.obt_lessons', 'lesson_date',
     '["subject", "tutor_qualification"]'::jsonb,
     NULL, 'RUB', 'human', 'verified'),

    ('homework_rate', 'Сдача домашних работ',
     'Доля сданных домашних работ от выданных.',
     'countIf(is_submitted = 1) / count()', 'core.fct_homework', 'assigned_date',
     '[]'::jsonb, NULL, 'доля', 'human', 'verified'),

    ('avg_homework_score', 'Средний балл за ДЗ',
     'Средний процент за проверенные домашние работы. Непроверенные игнорируются.',
     'avg(score_pct)', 'core.fct_homework', 'assigned_date',
     '[]'::jsonb, NULL, '%', 'human', 'verified'),

    ('exam_pass_rate', 'Доля сдавших экзамен',
     'Доля экзаменов, сданных выше порога. Главный показатель результата обучения.',
     'countIf(is_passed = 1) / count()', 'core.fct_exam', 'exam_date',
     '["subject", "exam_type"]'::jsonb, NULL, 'доля', 'human', 'verified'),

    ('avg_exam_score', 'Средний балл экзамена',
     'Средний процент за экзамен.',
     'avg(score_pct)', 'core.fct_exam', 'exam_date',
     '["subject", "exam_type"]'::jsonb, NULL, '%', 'human', 'verified')
ON CONFLICT (name) DO UPDATE
   SET sql_expression = EXCLUDED.sql_expression,
       base_table = EXCLUDED.base_table,
       time_column = EXCLUDED.time_column,
       dimensions = EXCLUDED.dimensions,
       description = EXCLUDED.description,
       origin = 'human', status = 'verified', updated_at = now();

-- ── связи ─────────────────────────────────────────────────────────────────────
DELETE FROM sem.relations WHERE from_table LIKE '%order%' OR to_table LIKE '%customer%';

INSERT INTO sem.relations
    (from_table, from_columns, to_table, to_columns, relation_type, notes, origin, status)
VALUES
    ('core.fct_lesson', ARRAY['student_id'], 'core.dim_student', ARRAY['student_id'],
     'many_to_one', 'Занятие к ученику.', 'human', 'verified'),
    ('core.fct_lesson', ARRAY['tutor_id'], 'core.dim_tutor', ARRAY['tutor_id'],
     'many_to_one', 'Занятие к репетитору.', 'human', 'verified'),
    ('core.fct_homework', ARRAY['lesson_id'], 'core.fct_lesson', ARRAY['lesson_id'],
     'many_to_one', 'Домашняя работа выдаётся на занятии.', 'human', 'verified'),
    ('core.fct_exam', ARRAY['student_id'], 'core.dim_student', ARRAY['student_id'],
     'many_to_one', 'Экзамен сдаёт ученик. К занятиям напрямую не привязан — только через ученика.',
     'human', 'verified')
ON CONFLICT (from_table, to_table, from_columns, to_columns) DO NOTHING;
