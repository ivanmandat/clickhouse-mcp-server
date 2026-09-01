-- Преобразования по слоям для домена репетиторства: int → core → mart.
-- Алиасы агрегатов НЕ совпадают с именами колонок: иначе _ingested_at внутри
-- argMax() разрешается в алиас и получается агрегат внутри агрегата.

-- ═══ raw → int ════════════════════════════════════════════════════════════════

TRUNCATE TABLE IF EXISTS `int`.students;
INSERT INTO `int`.students
    (student_id, full_name, email, grade, city, signup_at, status, is_test, _ingested_at)
SELECT
    toUInt64(student_id),
    argMax(full_name, _ingested_at),
    argMax(email, _ingested_at),
    toUInt8OrNull(argMax(grade, _ingested_at)),
    argMax(city, _ingested_at),
    parseDateTimeBestEffort(argMax(signup_ts, _ingested_at)),
    lower(argMax(status, _ingested_at)),
    if(argMax(is_test, _ingested_at) IN ('1', 'true'), 1, 0),
    max(_ingested_at) AS _ingested_at_max
FROM raw.crm_students
GROUP BY student_id;

TRUNCATE TABLE IF EXISTS `int`.tutors;
INSERT INTO `int`.tutors
    (tutor_id, full_name, subject, qualification, hourly_rate, hired_at, is_active, _ingested_at)
SELECT
    toUInt64(tutor_id),
    argMax(full_name, _ingested_at),
    argMax(subject, _ingested_at),
    argMax(qualification, _ingested_at),
    toDecimal64(argMax(hourly_rate, _ingested_at), 2),
    parseDateTimeBestEffort(argMax(hired_ts, _ingested_at)),
    if(argMax(is_active, _ingested_at) = '1', 1, 0),
    max(_ingested_at) AS _ingested_at_max
FROM raw.crm_tutors
GROUP BY tutor_id;

TRUNCATE TABLE IF EXISTS `int`.lessons;
INSERT INTO `int`.lessons
    (lesson_id, student_id, tutor_id, subject, scheduled_at, status,
     duration_min, price, payment_status, format, _ingested_at)
SELECT
    toUInt64(lesson_id),
    toUInt64(argMax(student_id, _ingested_at)),
    toUInt64(argMax(tutor_id, _ingested_at)),
    argMax(subject, _ingested_at),
    parseDateTimeBestEffort(argMax(scheduled_ts, _ingested_at)),
    lower(argMax(status, _ingested_at)),
    toUInt16(argMax(duration_min, _ingested_at)),
    toDecimal64(argMax(price, _ingested_at), 2),
    lower(argMax(payment_status, _ingested_at)),
    argMax(format, _ingested_at),
    max(_ingested_at) AS _ingested_at_max
FROM raw.crm_lessons
GROUP BY lesson_id;

TRUNCATE TABLE IF EXISTS `int`.homework;
INSERT INTO `int`.homework
    (homework_id, lesson_id, student_id, assigned_at, due_at, submitted_at,
     score, max_score, status, _ingested_at)
SELECT
    cityHash64(homework_id),
    toUInt64(lesson_id),
    toUInt64(student_id),
    parseDateTimeBestEffort(assigned_ts),
    parseDateTimeBestEffort(due_ts),
    parseDateTimeBestEffortOrNull(submitted_ts),
    toDecimal64OrNull(score, 2),
    toDecimal64(max_score, 2),
    status,
    _ingested_at
FROM raw.crm_homework;

TRUNCATE TABLE IF EXISTS `int`.exams;
INSERT INTO `int`.exams
    (exam_id, student_id, subject, exam_type, exam_at, score, max_score,
     passing_score, attempt_no, _ingested_at)
SELECT
    cityHash64(exam_id),
    toUInt64(student_id),
    subject,
    exam_type,
    parseDateTimeBestEffort(exam_ts),
    toDecimal64(score, 2),
    toDecimal64(max_score, 2),
    toDecimal64(passing_score, 2),
    toUInt8(attempt_no),
    _ingested_at
FROM raw.crm_exams;

-- ═══ int → core ═══════════════════════════════════════════════════════════════
-- Здесь отсекаются тестовые ученики. Правило бизнесовое, из схемы не выводится.

TRUNCATE TABLE IF EXISTS core.dim_student;
INSERT INTO core.dim_student
    (student_id, full_name, email, grade, city, signup_at, signup_date, status)
SELECT student_id, full_name, email, grade, city, signup_at,
       toDate(signup_at), status
FROM `int`.students FINAL
WHERE is_test = 0;

TRUNCATE TABLE IF EXISTS core.dim_tutor;
INSERT INTO core.dim_tutor
    (tutor_id, full_name, subject, qualification, hourly_rate, hired_at, is_active)
SELECT tutor_id, full_name, subject, qualification, hourly_rate, hired_at, is_active
FROM `int`.tutors FINAL;

TRUNCATE TABLE IF EXISTS core.fct_lesson;
INSERT INTO core.fct_lesson
    (lesson_id, student_id, tutor_id, subject, scheduled_at, lesson_date, status,
     payment_status, format, duration_min, price, is_completed, is_paid)
SELECT
    l.lesson_id, l.student_id, l.tutor_id, l.subject, l.scheduled_at,
    toDate(l.scheduled_at) AS lesson_date,
    l.status, l.payment_status, l.format, l.duration_min, l.price,
    if(l.status = 'completed', 1, 0) AS is_completed,
    if(l.payment_status = 'paid', 1, 0) AS is_paid
FROM `int`.lessons AS l FINAL
INNER JOIN (SELECT student_id FROM `int`.students FINAL WHERE is_test = 0) AS s
        ON s.student_id = l.student_id;

TRUNCATE TABLE IF EXISTS core.fct_homework;
INSERT INTO core.fct_homework
    (homework_id, lesson_id, student_id, assigned_at, assigned_date, due_at,
     submitted_at, score, max_score, score_pct, status, is_submitted, is_on_time)
SELECT
    h.homework_id, h.lesson_id, h.student_id, h.assigned_at,
    toDate(h.assigned_at) AS assigned_date,
    h.due_at, h.submitted_at, h.score, h.max_score,
    if(isNull(h.score), NULL, toFloat64(h.score) / toFloat64(h.max_score) * 100) AS score_pct,
    h.status,
    if(isNotNull(h.submitted_at), 1, 0) AS is_submitted,
    if(isNotNull(h.submitted_at) AND h.submitted_at <= h.due_at, 1, 0) AS is_on_time
FROM `int`.homework AS h FINAL
INNER JOIN (SELECT student_id FROM `int`.students FINAL WHERE is_test = 0) AS s
        ON s.student_id = h.student_id;

TRUNCATE TABLE IF EXISTS core.fct_exam;
INSERT INTO core.fct_exam
    (exam_id, student_id, subject, exam_type, exam_at, exam_date, score, max_score,
     passing_score, score_pct, attempt_no, is_passed)
SELECT
    e.exam_id, e.student_id, e.subject, e.exam_type, e.exam_at,
    toDate(e.exam_at) AS exam_date,
    e.score, e.max_score, e.passing_score,
    toFloat64(e.score) / toFloat64(e.max_score) * 100 AS score_pct,
    e.attempt_no,
    if(e.score >= e.passing_score, 1, 0) AS is_passed
FROM `int`.exams AS e FINAL
INNER JOIN (SELECT student_id FROM `int`.students FINAL WHERE is_test = 0) AS s
        ON s.student_id = e.student_id;

-- ═══ core → mart ══════════════════════════════════════════════════════════════

TRUNCATE TABLE IF EXISTS mart.obt_lessons;
INSERT INTO mart.obt_lessons
    (lesson_id, student_id, tutor_id, lesson_date, scheduled_at, lesson_month,
     subject, status, payment_status, format,
     student_city, student_grade, student_status,
     tutor_name, tutor_qualification,
     duration_min, price, revenue, is_completed, is_paid, is_no_show)
SELECT
    f.lesson_id, f.student_id, f.tutor_id, f.lesson_date, f.scheduled_at,
    toStartOfMonth(f.lesson_date) AS lesson_month,
    f.subject, f.status, f.payment_status, f.format,
    ds.city, ds.grade, ds.status,
    dt.full_name, dt.qualification,
    f.duration_min, f.price,
    if(f.is_completed = 1 AND f.is_paid = 1, f.price, toDecimal64(0, 2)) AS revenue,
    f.is_completed, f.is_paid,
    if(f.status = 'no_show', 1, 0) AS is_no_show
FROM core.fct_lesson AS f
INNER JOIN core.dim_student AS ds ON ds.student_id = f.student_id
INNER JOIN core.dim_tutor   AS dt ON dt.tutor_id   = f.tutor_id;

TRUNCATE TABLE IF EXISTS mart.daily_lessons;
INSERT INTO mart.daily_lessons
    (lesson_date, subject, format, qualification, lessons_total, lessons_completed,
     lessons_no_show, students_count, tutors_count, revenue, completion_rate)
SELECT
    lesson_date, subject, format,
    tutor_qualification AS qualification,
    count()                              AS lessons_total,
    countIf(is_completed = 1)            AS lessons_completed,
    countIf(is_no_show = 1)              AS lessons_no_show,
    uniqExact(student_id)                AS students_count,
    uniqExact(tutor_id)                  AS tutors_count,
    toDecimal64(sum(revenue), 2)         AS revenue,
    countIf(is_completed = 1) / count()  AS completion_rate
FROM mart.obt_lessons
GROUP BY lesson_date, subject, format, qualification;

TRUNCATE TABLE IF EXISTS mart.student_progress;
INSERT INTO mart.student_progress
    (student_id, full_name, city, grade, status, signup_date,
     lessons_total, lessons_completed, lessons_no_show, attendance_rate, revenue_total,
     homework_assigned, homework_submitted, homework_on_time, homework_rate,
     avg_homework_score, exams_taken, exams_passed, avg_exam_score,
     first_lesson_date, last_lesson_date)
SELECT
    d.student_id, d.full_name, d.city, d.grade, d.status, d.signup_date,
    ifNull(l.total, 0), ifNull(l.done, 0), ifNull(l.miss, 0),
    if(ifNull(l.total, 0) = 0, 0, l.done / l.total) AS attendance_rate,
    toDecimal64(ifNull(l.rev, 0), 2),
    ifNull(h.assigned, 0), ifNull(h.submitted, 0), ifNull(h.on_time, 0),
    if(ifNull(h.assigned, 0) = 0, 0, h.submitted / h.assigned) AS homework_rate,
    h.avg_score,
    ifNull(e.taken, 0), ifNull(e.passed, 0), e.avg_score,
    l.first_date, l.last_date
FROM core.dim_student AS d
LEFT JOIN
(
    SELECT student_id,
           count() AS total, countIf(is_completed = 1) AS done,
           countIf(is_no_show = 1) AS miss, sum(revenue) AS rev,
           min(lesson_date) AS first_date, max(lesson_date) AS last_date
    FROM mart.obt_lessons GROUP BY student_id
) AS l USING (student_id)
LEFT JOIN
(
    SELECT student_id, count() AS assigned, countIf(is_submitted = 1) AS submitted,
           countIf(is_on_time = 1) AS on_time, avg(score_pct) AS avg_score
    FROM core.fct_homework GROUP BY student_id
) AS h USING (student_id)
LEFT JOIN
(
    SELECT student_id, count() AS taken, countIf(is_passed = 1) AS passed,
           avg(score_pct) AS avg_score
    FROM core.fct_exam GROUP BY student_id
) AS e USING (student_id)
SETTINGS join_use_nulls = 1;

TRUNCATE TABLE IF EXISTS mart.tutor_performance;
INSERT INTO mart.tutor_performance
    (tutor_id, full_name, subject, qualification, lessons_total, lessons_completed,
     lessons_no_show, completion_rate, students_count, revenue_total,
     avg_student_homework_score, exam_pass_rate)
SELECT
    t.tutor_id, t.full_name, t.subject, t.qualification,
    ifNull(l.total, 0), ifNull(l.done, 0), ifNull(l.miss, 0),
    if(ifNull(l.total, 0) = 0, 0, l.done / l.total) AS completion_rate,
    ifNull(l.students, 0),
    toDecimal64(ifNull(l.rev, 0), 2),
    r.avg_hw, r.pass_rate
FROM core.dim_tutor AS t
LEFT JOIN
(
    SELECT tutor_id, count() AS total, countIf(is_completed = 1) AS done,
           countIf(is_no_show = 1) AS miss, uniqExact(student_id) AS students,
           sum(revenue) AS rev
    FROM mart.obt_lessons GROUP BY tutor_id
) AS l USING (tutor_id)
LEFT JOIN
(
    -- результаты учеников этого репетитора
    SELECT tutor_id, avg(hw) AS avg_hw, avg(passed) AS pass_rate
    FROM
    (
        SELECT DISTINCT o.tutor_id AS tutor_id, o.student_id AS student_id,
               sp.avg_homework_score AS hw,
               if(sp.exams_taken = 0, NULL, sp.exams_passed / sp.exams_taken) AS passed
        FROM mart.obt_lessons AS o
        INNER JOIN mart.student_progress AS sp ON sp.student_id = o.student_id
    )
    GROUP BY tutor_id
) AS r USING (tutor_id)
SETTINGS join_use_nulls = 1;

TRUNCATE TABLE IF EXISTS mart.exam_outcomes;
INSERT INTO mart.exam_outcomes
    (exam_date, exam_month, subject, exam_type, student_grade, exams_taken,
     exams_passed, pass_rate, avg_score_pct, lessons_before_exam_avg)
SELECT
    e.exam_date,
    toStartOfMonth(e.exam_date) AS exam_month,
    e.subject, e.exam_type, d.grade AS student_grade,
    count()                             AS exams_taken,
    countIf(e.is_passed = 1)            AS exams_passed,
    countIf(e.is_passed = 1) / count()  AS pass_rate,
    avg(e.score_pct)                    AS avg_score_pct,
    avg(ifNull(sp.lessons_completed, 0)) AS lessons_before_exam_avg
FROM core.fct_exam AS e
INNER JOIN core.dim_student AS d ON d.student_id = e.student_id
LEFT JOIN mart.student_progress AS sp ON sp.student_id = e.student_id
GROUP BY e.exam_date, exam_month, e.subject, e.exam_type, student_grade
SETTINGS join_use_nulls = 1;
