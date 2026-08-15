-- Post-deploy: проверка/очистка после применения схемы.
-- Скрипт выполняется после применения схемы; идемпотентен (CDF-1).
DELETE FROM app.tmp_stage WHERE id IS NULL;
