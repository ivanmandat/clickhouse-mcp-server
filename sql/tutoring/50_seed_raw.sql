-- Наполнение raw для домена репетиторства.
-- Период: 2024-01-01 .. 2025-08-31. Тренд роста, просадка летних месяцев,
-- корреляция: чем больше занятий и лучше домашние работы — тем выше шанс
-- сдать экзамен. Это нужно, чтобы дашборды показывали осмысленные связи.

-- ── ученики ───────────────────────────────────────────────────────────────────
INSERT INTO raw.crm_students
SELECT
    now64(3) - toIntervalSecond(number % 3600) AS _ingested_at,
    'crm' AS _source, 'seed-001' AS _batch_id,
    toString(10000 + number) AS student_id,
    concat(arrayElement(['Артём','Софья','Егор','Полина','Матвей','Варвара','Тимур','Алиса'],
                        toInt32(intHash32(number) % 8) + 1),
           ' ',
           arrayElement(['Морозов','Лебедева','Козлов','Новикова','Орлов','Зайцева'],
                        toInt32(intHash32(number + 5) % 6) + 1)) AS full_name,
    concat('student', toString(10000 + number), '@example.com') AS email,
    -- 15% взрослых учеников приходят без класса
    if(number % 7 = 0, '', toString(5 + intHash32(number + 2) % 7)) AS grade,
    arrayElement(['Москва','Санкт-Петербург','Казань','Новосибирск','Екатеринбург','Краснодар'],
                 toInt32(intHash32(number + 3) % 6) + 1) AS city,
    toString(toDateTime('2023-09-01 00:00:00')
             + toIntervalSecond(intHash32(number + 11) % 55000000)) AS signup_ts,
    arrayElement(['ACTIVE','active','Active','PAUSED','paused','churned'],
                 toInt32(intHash32(number + 17) % 6) + 1) AS status,
    if(number % 89 = 0, '1', '0') AS is_test
FROM numbers(3000);

-- повторная загрузка части: дубли по ключу
INSERT INTO raw.crm_students
SELECT now64(3), _source, 'seed-002', student_id, full_name, email,
       grade, city, signup_ts, lower(status), is_test
FROM raw.crm_students WHERE cityHash64(student_id) % 29 = 0;

-- ── репетиторы ────────────────────────────────────────────────────────────────
INSERT INTO raw.crm_tutors
SELECT
    now64(3) AS _ingested_at, 'crm' AS _source, 'seed-001' AS _batch_id,
    toString(500 + number) AS tutor_id,
    concat(arrayElement(['Мария','Андрей','Ольга','Дмитрий','Наталья','Игорь'],
                        toInt32(intHash32(number) % 6) + 1),
           ' ',
           arrayElement(['Соколова','Ковалёв','Белова','Романов','Гусева','Титов'],
                        toInt32(intHash32(number + 4) % 6) + 1)) AS full_name,
    arrayElement(['Математика','Русский язык','Английский','Физика','Химия','Биология','Информатика','Обществознание'],
                 toInt32(intHash32(number + 1) % 8) + 1) AS subject,
    arrayElement(['junior','middle','middle','senior','senior','expert'],
                 toInt32(intHash32(number + 6) % 6) + 1) AS qualification,
    toString(round(800 + (cityHash64(toString(500 + number)) % 220000) / 100, 2)) AS hourly_rate,
    toString(toDateTime('2022-01-01 00:00:00')
             + toIntervalSecond(intHash32(number + 9) % 94000000)) AS hired_ts,
    if(number % 19 = 0, '0', '1') AS is_active
FROM numbers(120);

-- ── занятия ───────────────────────────────────────────────────────────────────
INSERT INTO raw.crm_lessons
SELECT
    now64(3) - toIntervalSecond(number % 7200) AS _ingested_at,
    'crm' AS _source, 'seed-001' AS _batch_id,
    toString(1000000 + number) AS lesson_id,
    toString(10000 + sid) AS student_id,
    toString(500 + tid) AS tutor_id,
    subj AS subject,
    toString(sched) AS scheduled_ts,
    st AS status,
    toString(dur) AS duration_min,
    toString(round(dur / 60.0 * rate, 2)) AS price,
    pay AS payment_status,
    fmt AS format
FROM
(
    SELECT
        number, sched, sid, tid, subj, dur,
        -- ставка репетитора выводится из его id детерминированно
        round(800 + (cityHash64(toString(500 + tid)) % 220000) / 100, 2) AS rate,
        multiIf(r < 82, 'completed',
                r < 89, 'cancelled',
                r < 95, 'no_show',
                        'rescheduled') AS st,
        multiIf(pr < 88, 'paid', pr < 96, 'pending', 'refunded') AS pay,
        arrayElement(['online','online','online','offline'],
                     toInt32(cityHash64(number, 'fmt') % 4) + 1) AS fmt
    FROM
    (
        SELECT
            number,
            toDateTime('2024-01-01 00:00:00')
                + toIntervalDay(toUInt32(608 * pow((cityHash64(number, 'day') % 1000000) / 1000000.0, 0.72)))
                + toIntervalSecond(32400 + cityHash64(number, 'sec') % 39600) AS sched,
            -- показатель 2.2 создаёт длинный хвост: у части учеников десятки
            -- занятий, у части единицы. Равномерное распределение давало всем
            -- поровну, и связь «занятия -> результат» вырождалась в плоскую.
            toUInt32(2999 * pow((cityHash64(number, 'st') % 1000000) / 1000000.0, 2.2)) AS sid,
            cityHash64(number, 'tu') % 120  AS tid,
            arrayElement(['Математика','Русский язык','Английский','Физика','Химия','Биология','Информатика','Обществознание'],
                         toInt32(cityHash64(number, 'sub') % 8) + 1) AS subj,
            arrayElement([45, 60, 60, 60, 90], toInt32(cityHash64(number, 'dur') % 5) + 1) AS dur,
            toInt32(cityHash64(number, 'stt') % 100) AS r,
            toInt32(cityHash64(number, 'pay') % 100) AS pr
        FROM numbers(260000)
    )
)
-- летняя просадка: в июле и августе занятий заметно меньше
WHERE NOT (toMonth(sched) IN (7, 8) AND cityHash64(number, 'sum') % 100 < 55);

-- ── домашние работы ───────────────────────────────────────────────────────────
-- Выдаются примерно к 70% состоявшихся занятий.
INSERT INTO raw.crm_homework
SELECT
    now64(3) AS _ingested_at, 'lms' AS _source, 'seed-001' AS _batch_id,
    concat('h', lesson_id) AS homework_id,
    lesson_id,
    student_id,
    toString(assigned) AS assigned_ts,
    toString(assigned + toIntervalDay(7)) AS due_ts,
    if(submitted = 1, toString(assigned + toIntervalHour(12 + delay)), '') AS submitted_ts,
    if(submitted = 1, toString(sc), '') AS score,
    '100' AS max_score,
    multiIf(submitted = 0 AND assigned + toIntervalDay(7) < now(), 'overdue',
            submitted = 0, 'assigned',
            'graded') AS status
FROM
(
    SELECT
        l.lesson_id AS lesson_id,
        l.student_id AS student_id,
        parseDateTimeBestEffort(l.scheduled_ts) AS assigned,
        if(cityHash64(l.lesson_id, 'sub') % 100 < 78, 1, 0) AS submitted,
        toInt32(cityHash64(l.lesson_id, 'dl') % 160) AS delay,
        -- балл зависит от ученика: у части учеников стабильно выше
        round(least(100, greatest(20,
            45 + (cityHash64(l.student_id, 'skill') % 45)
               + (toInt32(cityHash64(l.lesson_id, 'noise') % 21) - 10))), 0) AS sc
    FROM raw.crm_lessons AS l
    WHERE l.status = 'completed'
      AND cityHash64(l.lesson_id, 'hw') % 100 < 70
);

-- ── экзамены ──────────────────────────────────────────────────────────────────
-- Сдают около трети учеников; результат коррелирует с числом занятий.
INSERT INTO raw.crm_exams
SELECT
    now64(3) AS _ingested_at, 'crm' AS _source, 'seed-001' AS _batch_id,
    concat('e', toString(student_id), '-', toString(attempt)) AS exam_id,
    toString(student_id) AS student_id,
    subj AS subject,
    et AS exam_type,
    toString(exam_at) AS exam_ts,
    toString(sc) AS score,
    '100' AS max_score,
    '40' AS passing_score,
    toString(attempt) AS attempt_no
FROM
(
    SELECT
        toUInt64(s.student_id) AS student_id,
        cnt,
        1 + (cityHash64(s.student_id, 'att') % 2) AS attempt,
        arrayElement(['Математика','Русский язык','Английский','Физика','Обществознание'],
                     toInt32(cityHash64(s.student_id, 'esub') % 5) + 1) AS subj,
        arrayElement(['ЕГЭ','ОГЭ','внутренний','внутренний'],
                     toInt32(cityHash64(s.student_id, 'et') % 4) + 1) AS et,
        toDateTime('2024-05-20 00:00:00')
            + toIntervalDay(toInt32(cityHash64(s.student_id, 'ed') % 460)) AS exam_at,
        -- 18 базовых + до 52 за занятия + шум. При пороге 40 ученик с десятком
        -- занятий чаще не сдаёт, а с сотней — почти всегда сдаёт.
        round(least(100, greatest(3,
            18 + least(52, cnt * 0.62)
               + (toInt32(cityHash64(s.student_id, 'en') % 25) - 12))), 0) AS sc
    FROM
    (
        SELECT student_id, count() AS cnt
        FROM raw.crm_lessons
        WHERE status = 'completed'
        GROUP BY student_id
    ) AS s
    WHERE cityHash64(s.student_id, 'exam') % 100 < 34
);
