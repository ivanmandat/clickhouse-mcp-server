-- Домен: репетиторство. Слой raw — как пришло из источников.
-- Продажи здесь это занятия с репетитором, плюс результаты обучения:
-- домашние работы и экзамены.

DROP TABLE IF EXISTS raw.crm_students;
CREATE TABLE raw.crm_students
(
    _ingested_at DateTime64(3),
    _source      LowCardinality(String),
    _batch_id    String,

    student_id   String COMMENT 'Идентификатор ученика в CRM',
    full_name    String,
    email        String,
    grade        String COMMENT 'Класс обучения, 5..11; пустая строка для взрослых',
    city         String,
    signup_ts    String,
    status       String COMMENT 'Разнобой регистра: ACTIVE / active / PAUSED / churned',
    is_test      String
)
ENGINE = MergeTree ORDER BY (student_id, _ingested_at)
COMMENT 'Сырая выгрузка учеников из CRM. Дубли по student_id ожидаемы.';

DROP TABLE IF EXISTS raw.crm_tutors;
CREATE TABLE raw.crm_tutors
(
    _ingested_at DateTime64(3),
    _source      LowCardinality(String),
    _batch_id    String,

    tutor_id     String,
    full_name    String,
    subject      String COMMENT 'Основной предмет',
    qualification String COMMENT 'junior / middle / senior / expert',
    hourly_rate  String COMMENT 'Ставка за академический час, строкой',
    hired_ts     String,
    is_active    String
)
ENGINE = MergeTree ORDER BY (tutor_id, _ingested_at)
COMMENT 'Сырая выгрузка репетиторов.';

DROP TABLE IF EXISTS raw.crm_lessons;
CREATE TABLE raw.crm_lessons
(
    _ingested_at DateTime64(3),
    _source      LowCardinality(String),
    _batch_id    String,

    lesson_id    String,
    student_id   String,
    tutor_id     String,
    subject      String,
    scheduled_ts String COMMENT 'Плановое время занятия',
    status       String COMMENT 'completed / cancelled / no_show / rescheduled',
    duration_min String COMMENT 'Длительность в минутах: 45, 60, 90',
    price        String COMMENT 'Стоимость занятия',
    payment_status String COMMENT 'paid / pending / refunded',
    format       String COMMENT 'online / offline'
)
ENGINE = MergeTree ORDER BY (lesson_id, _ingested_at)
COMMENT 'Сырые занятия. Это основная транзакционная сущность: аналог заказа.';

DROP TABLE IF EXISTS raw.crm_homework;
CREATE TABLE raw.crm_homework
(
    _ingested_at DateTime64(3),
    _source      LowCardinality(String),
    _batch_id    String,

    homework_id  String,
    lesson_id    String,
    student_id   String,
    assigned_ts  String,
    due_ts       String,
    submitted_ts String COMMENT 'Пустая строка, если не сдана',
    score        String COMMENT 'Балл; пустая строка, если не проверена',
    max_score    String,
    status       String COMMENT 'assigned / submitted / graded / overdue'
)
ENGINE = MergeTree ORDER BY (homework_id, _ingested_at)
COMMENT 'Сырые домашние работы. Привязаны к занятию.';

DROP TABLE IF EXISTS raw.crm_exams;
CREATE TABLE raw.crm_exams
(
    _ingested_at DateTime64(3),
    _source      LowCardinality(String),
    _batch_id    String,

    exam_id      String,
    student_id   String,
    subject      String,
    exam_type    String COMMENT 'ЕГЭ / ОГЭ / внутренний',
    exam_ts      String,
    score        String,
    max_score    String,
    passing_score String COMMENT 'Порог сдачи',
    attempt_no   String
)
ENGINE = MergeTree ORDER BY (exam_id, _ingested_at)
COMMENT 'Сырые результаты экзаменов — итог обучения.';
