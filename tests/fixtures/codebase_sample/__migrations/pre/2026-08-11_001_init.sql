-- Pre-deploy: идемпотентная подготовка места под будущие данные.
-- Скрипт выполняется перед применением схемы; идемпотентен (CDF-1).
-- Pre-scripts run BEFORE user objects: the schema must be created here if
-- missing (self-sufficient — the fresh temp DB has no user schemas yet).
CREATE SCHEMA IF NOT EXISTS app;
CREATE TABLE IF NOT EXISTS app.tmp_stage (id int);
