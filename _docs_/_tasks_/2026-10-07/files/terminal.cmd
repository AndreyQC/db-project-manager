@echo off
rem Терминал в папке бандла: dbpm.cmd и dbpm_profile_hrdo.cmd ищут
rem connections\ в текущем каталоге, поэтому команды запускаются отсюда.
title dbpm-portable
cd /d "%~dp0"
cmd /k
