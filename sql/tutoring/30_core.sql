-- Слой core: очищенные факты и измерения. Тестовые ученики уже исключены.

DROP TABLE IF EXISTS core.dim_student;
CREATE TABLE core.dim_student
(
    student_id  UInt64,
    full_name   String,
    email       String,
    grade       Nullable(UInt8),
    city        LowCardinality(String),
    signup_at   DateTime,
    signup_date Date,
    status      LowCardinality(String) COMMENT 'active / paused / churned'
)
ENGINE = MergeTree ORDER BY student_id
COMMENT 'Измерение: ученики. Тестовые аккаунты исключены.';

DROP TABLE IF EXISTS core.dim_tutor;
CREATE TABLE core.dim_tutor
(
    tutor_id      UInt64,
    full_name     String,
    subject       LowCardinality(String),
    qualification LowCardinality(String) COMMENT 'junior / middle / senior / expert',
    hourly_rate   Decimal(10, 2),
    hired_at      DateTime,
    is_active     UInt8
)
ENGINE = MergeTree ORDER BY tutor_id
COMMENT 'Измерение: репетиторы.';

DROP TABLE IF EXISTS core.fct_lesson;
CREATE TABLE core.fct_lesson
(
    lesson_id      UInt64,
    student_id     UInt64,
    tutor_id       UInt64,
    subject        LowCardinality(String),
    scheduled_at   DateTime,
    lesson_date    Date,
    status         LowCardinality(String) COMMENT 'completed / cancelled / no_show / rescheduled',
    payment_status LowCardinality(String),
    format         LowCardinality(String),
    duration_min   UInt16,
    price          Decimal(12, 2) COMMENT 'Выручкой считается только при status=completed и payment_status=paid',
    is_completed   UInt8 COMMENT '1, если занятие состоялось',
    is_paid        UInt8
)
ENGINE = MergeTree PARTITION BY toYYYYMM(lesson_date) ORDER BY (lesson_date, lesson_id)
COMMENT 'Факт: занятие. Основная транзакционная гранулярность.';

DROP TABLE IF EXISTS core.fct_homework;
CREATE TABLE core.fct_homework
(
    homework_id   UInt64,
    lesson_id     UInt64,
    student_id    UInt64,
    assigned_at   DateTime,
    assigned_date Date,
    due_at        DateTime,
    submitted_at  Nullable(DateTime),
    score         Nullable(Decimal(6, 2)),
    max_score     Decimal(6, 2),
    score_pct     Nullable(Float64) COMMENT 'score / max_score * 100',
    status        LowCardinality(String),
    is_submitted  UInt8,
    is_on_time    UInt8 COMMENT '1, если сдана до due_at'
)
ENGINE = MergeTree PARTITION BY toYYYYMM(assigned_date) ORDER BY (assigned_date, homework_id)
COMMENT 'Факт: домашняя работа.';

DROP TABLE IF EXISTS core.fct_exam;
CREATE TABLE core.fct_exam
(
    exam_id       UInt64,
    student_id    UInt64,
    subject       LowCardinality(String),
    exam_type     LowCardinality(String) COMMENT 'ЕГЭ / ОГЭ / внутренний',
    exam_at       DateTime,
    exam_date     Date,
    score         Decimal(6, 2),
    max_score     Decimal(6, 2),
    passing_score Decimal(6, 2),
    score_pct     Float64,
    attempt_no    UInt8,
    is_passed     UInt8 COMMENT '1, если score >= passing_score'
)
ENGINE = MergeTree PARTITION BY toYYYYMM(exam_date) ORDER BY (exam_date, exam_id)
COMMENT 'Факт: экзамен. Итог обучения.';
