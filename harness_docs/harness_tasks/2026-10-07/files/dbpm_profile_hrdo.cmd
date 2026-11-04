@echo off
rem Профайлинг всех таблиц подключения hrdo_ods (маскированные ODS-схемы).
rem Инструкция: PROFILE-HRDO-ODS.md в этой папке.
"%~dp0runtime\python.exe" "%~dp0profile_hrdo_ods.py" %*
