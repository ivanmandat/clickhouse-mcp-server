-- Слой mart: OBT и витрины под дашборды.

DROP TABLE IF EXISTS mart.obt_lessons;
CREATE TABLE mart.obt_lessons
(
    lesson_id      UInt64,
    student_id     UInt64,
    tutor_id       UInt64,

    lesson_date    Date,
    scheduled_at   DateTime,
    lesson_month   Date COMMENT 'Первое число месяца занятия',

    subject        LowCardinality(String),
    status         LowCardinality(String) COMMENT 'completed / cancelled / no_show / rescheduled',
    payment_status LowCardinality(String),
    format         LowCardinality(String) COMMENT 'online / offline',

    student_city   LowCardinality(String),
    student_grade  Nullable(UInt8),
    student_status LowCardinality(String),

    tutor_name     String,
    tutor_qualification LowCardinality(String),

    duration_min   UInt16,
    price          Decimal(12, 2),
    revenue        Decimal(12, 2) COMMENT 'price при состоявшемся и оплаченном занятии, иначе 0',
    is_completed   UInt8,
    is_paid        UInt8,
    is_no_show     UInt8
)
ENGINE = MergeTree PARTITION BY toYYYYMM(lesson_date)
ORDER BY (lesson_date, subject, tutor_id, lesson_id)
COMMENT 'OBT занятий. Гранулярность: одна строка = одно занятие. Основная таблица для дашбордов.';

DROP TABLE IF EXISTS mart.daily_lessons;
CREATE TABLE mart.daily_lessons
(
    lesson_date     Date,
    subject         LowCardinality(String),
    format          LowCardinality(String),
    qualification   LowCardinality(String),

    lessons_total   UInt64,
    lessons_completed UInt64,
    lessons_no_show UInt64,
    students_count  UInt64,
    tutors_count    UInt64,
    revenue         Decimal(18, 2),
    completion_rate Float64 COMMENT 'lessons_completed / lessons_total'
)
ENGINE = MergeTree PARTITION BY toYYYYMM(lesson_date)
ORDER BY (lesson_date, subject, format, qualification)
COMMENT 'Агрегат занятий по дню и трём измерениям. Быстрый источник для трендов.';

DROP TABLE IF EXISTS mart.student_progress;
CREATE TABLE mart.student_progress
(
    student_id        UInt64,
    full_name         String,
    city              LowCardinality(String),
    grade             Nullable(UInt8),
    status            LowCardinality(String),
    signup_date       Date,

    lessons_total     UInt32,
    lessons_completed UInt32,
    lessons_no_show   UInt32,
    attendance_rate   Float64 COMMENT 'lessons_completed / lessons_total',
    revenue_total     Decimal(18, 2),

    homework_assigned UInt32,
    homework_submitted UInt32,
    homework_on_time  UInt32,
    homework_rate     Float64 COMMENT 'Доля сданных домашних работ',
    avg_homework_score Nullable(Float64) COMMENT 'Средний процент за домашние работы',

    exams_taken       UInt32,
    exams_passed      UInt32,
    avg_exam_score    Nullable(Float64),

    first_lesson_date Nullable(Date),
    last_lesson_date  Nullable(Date)
)
ENGINE = MergeTree ORDER BY student_id
COMMENT 'Витрина ученика: посещаемость, домашние работы, экзамены. Гранулярность — один ученик.';

DROP TABLE IF EXISTS mart.tutor_performance;
CREATE TABLE mart.tutor_performance
(
    tutor_id          UInt64,
    full_name         String,
    subject           LowCardinality(String),
    qualification     LowCardinality(String),

    lessons_total     UInt32,
    lessons_completed UInt32,
    lessons_no_show   UInt32,
    completion_rate   Float64,
    students_count    UInt32,
    revenue_total     Decimal(18, 2),

    avg_student_homework_score Nullable(Float64) COMMENT 'Средний балл ДЗ у учеников репетитора',
    exam_pass_rate    Nullable(Float64) COMMENT 'Доля сдавших экзамен среди его учеников'
)
ENGINE = MergeTree ORDER BY tutor_id
COMMENT 'Витрина репетитора: нагрузка, выручка и результаты его учеников.';

DROP TABLE IF EXISTS mart.exam_outcomes;
CREATE TABLE mart.exam_outcomes
(
    exam_date       Date,
    exam_month      Date,
    subject         LowCardinality(String),
    exam_type       LowCardinality(String),
    student_grade   Nullable(UInt8),

    exams_taken     UInt64,
    exams_passed    UInt64,
    pass_rate       Float64,
    avg_score_pct   Float64,
    lessons_before_exam_avg Float64 COMMENT 'Среднее число занятий ученика до экзамена'
)
ENGINE = MergeTree PARTITION BY toYYYYMM(exam_date)
ORDER BY (exam_date, subject, exam_type)
COMMENT 'Итоги обучения: сдача экзаменов в разрезе предмета и типа.';
