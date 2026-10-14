@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
if exist ".venv\Scripts\python.exe" goto install
py -3.12 -c "import sys" >nul 2>&1
if not errorlevel 1 goto py312
py -3 -c "import sys; assert (3, 11) <= sys.version_info[:2] < (3, 15)" >nul 2>&1
if not errorlevel 1 goto py3
python -c "import sys; assert (3, 11) <= sys.version_info[:2] < (3, 15)" >nul 2>&1
if not errorlevel 1 goto python
echo Python 3.11-3.14 not found. Install Python 3.12 and enable Add Python to PATH.
echo https://www.python.org/downloads/windows/
pause
exit /b 1
:py312
py -3.12 -m venv .venv
goto checkvenv
:py3
py -3 -m venv .venv
goto checkvenv
:python
python -m venv .venv
:checkvenv
if errorlevel 1 goto failed
:install
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m unittest discover -s tests -v
if errorlevel 1 goto failed
if not exist ".env" copy /y ".env.example" ".env" >nul
echo.
echo Installation complete. Run configure.bat, then start.bat.
pause
exit /b 0
:failed
echo.
echo Installation failed. Read the error above. See README.html.
pause
exit /b 1
