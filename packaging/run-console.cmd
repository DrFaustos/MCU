@echo off
rem Запускает консольную сборку MCU-Client, сохраняет весь вывод (stdout+stderr,
rem включая нативные сообщения pjsua2/Qt) в out.txt и НЕ закрывает окно.
rem Нужно для диагностики нативного падения, которое в windowed-сборке не видно.
cd /d "%~dp0"
echo Запуск MCU-Client-console.exe ...
echo Вывод пишется в out.txt.
MCU-Client-console.exe > out.txt 2>&1
set EXITCODE=%ERRORLEVEL%
echo ----------------------------------------------
type out.txt
echo ----------------------------------------------
echo Процесс завершён с кодом %EXITCODE%.
echo Полный вывод сохранён в out.txt (в этой же папке).
pause
