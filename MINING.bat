@echo off
setlocal
cd /d "%~dp0"
set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=check"
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
if errorlevel 1 goto use_python
py -3 -X utf8 "%~dp0mine_guard.py" %ACTION%
goto finished
:use_python
python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
if errorlevel 1 goto missing
python -X utf8 "%~dp0mine_guard.py" %ACTION%
goto finished
:missing
echo Python 3.11+ is required: https://www.python.org/downloads/windows/
pause
exit /b 1
:finished
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" echo Mining guard stopped or setup is incomplete. Code: %RESULT%
if "%~1"=="" pause
exit /b %RESULT%
