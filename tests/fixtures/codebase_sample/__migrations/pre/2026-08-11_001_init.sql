-- Pre-deploy: идемпотентная подготовка места под будущие данные.
-- Скрипт выполняется перед применением схемы; идемпотентен (CDF-1).
CREATE TABLE IF NOT EXISTS app.tmp_stage (id int);
