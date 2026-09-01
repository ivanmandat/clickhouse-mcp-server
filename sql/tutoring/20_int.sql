-- Слой int: типизация, дедупликация, нормализация значений.

DROP TABLE IF EXISTS `int`.students;
CREATE TABLE `int`.students
(
    student_id   UInt64,
    full_name    String,
    email        String,
    grade        Nullable(UInt8) COMMENT 'NULL для взрослых учеников',
    city         LowCardinality(String),
    signup_at    DateTime,
    status       LowCardinality(String) COMMENT 'Нижний регистр: active / paused / churned',
    is_test      UInt8,
    _ingested_at DateTime64(3)
)
ENGINE = ReplacingMergeTree(_ingested_at) ORDER BY student_id
COMMENT 'Ученики: типизированы и дедуплицированы. Тестовые не отфильтрованы.';

DROP TABLE IF EXISTS `int`.tutors;
CREATE TABLE `int`.tutors
(
    tutor_id      UInt64,
    full_name     String,
    subject       LowCardinality(String),
    qualification LowCardinality(String),
    hourly_rate   Decimal(10, 2),
    hired_at      DateTime,
    is_active     UInt8,
    _ingested_at  DateTime64(3)
)
ENGINE = ReplacingMergeTree(_ingested_at) ORDER BY tutor_id
COMMENT 'Репетиторы: типизированы и дедуплицированы.';

DROP TABLE IF EXISTS `int`.lessons;
CREATE TABLE `int`.lessons
(
    lesson_id      UInt64,
    student_id     UInt64,
    tutor_id       UInt64,
    subject        LowCardinality(String),
    scheduled_at   DateTime,
    status         LowCardinality(String),
    duration_min   UInt16,
    price          Decimal(12, 2),
    payment_status LowCardinality(String),
    format         LowCardinality(String),
    _ingested_at   DateTime64(3)
)
ENGINE = ReplacingMergeTree(_ingested_at) ORDER BY lesson_id
COMMENT 'Занятия: типизированы. Отменённые и неявки не отфильтрованы.';

DROP TABLE IF EXISTS `int`.homework;
CREATE TABLE `int`.homework
(
    homework_id  UInt64,
    lesson_id    UInt64,
    student_id   UInt64,
    assigned_at  DateTime,
    due_at       DateTime,
    submitted_at Nullable(DateTime) COMMENT 'NULL, если не сдана',
    score        Nullable(Decimal(6, 2)),
    max_score    Decimal(6, 2),
    status       LowCardinality(String),
    _ingested_at DateTime64(3)
)
ENGINE = ReplacingMergeTree(_ingested_at) ORDER BY homework_id
COMMENT 'Домашние работы: типизированы, пустые строки превращены в NULL.';

DROP TABLE IF EXISTS `int`.exams;
CREATE TABLE `int`.exams
(
    exam_id       UInt64,
    student_id    UInt64,
    subject       LowCardinality(String),
    exam_type     LowCardinality(String),
    exam_at       DateTime,
    score         Decimal(6, 2),
    max_score     Decimal(6, 2),
    passing_score Decimal(6, 2),
    attempt_no    UInt8,
    _ingested_at  DateTime64(3)
)
ENGINE = ReplacingMergeTree(_ingested_at) ORDER BY exam_id
COMMENT 'Экзамены: типизированы. Признак сдачи считается в core.';
