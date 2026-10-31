@echo off
setlocal
call "%~dp0MINING.bat" run
set "RESULT=%ERRORLEVEL%"
echo.
echo Mining session finished. Exit code: %RESULT%
pause
exit /b %RESULT%
