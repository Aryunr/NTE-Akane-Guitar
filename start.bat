@echo off
setlocal
cd /d "%~dp0"
set "PIP_NO_INDEX=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
set "PYTHONPATH="
set "PYTHONHOME="

if exist ".venv\Scripts\python.exe" goto check_environment
py -3 -c "import sys,tkinter,venv,ensurepip; sys.exit(sys.version_info < (3,11))" >nul 2>&1
if not errorlevel 1 goto create_with_py
python -c "import sys,tkinter,venv,ensurepip; sys.exit(sys.version_info < (3,11))" >nul 2>&1
if errorlevel 1 goto missing_python
echo Creating local Python environment...
python -m venv --without-pip .venv
if errorlevel 1 goto failed
goto check_environment

:create_with_py
echo Creating local Python environment...
py -3 -m venv --without-pip .venv
if errorlevel 1 goto failed

:check_environment
".venv\Scripts\python.exe" -c "import sys,tkinter; sys.exit(sys.version_info < (3,11))" >nul 2>&1
if errorlevel 1 goto broken_environment
".venv\Scripts\python.exe" -c "import mido,pynput,packaging,six" >nul 2>&1
if not errorlevel 1 goto ready
echo Installing bundled dependencies OFFLINE. No network is used.
".venv\Scripts\python.exe" -m pip --version >nul 2>&1
if not errorlevel 1 goto install
".venv\Scripts\python.exe" -m ensurepip --default-pip
if errorlevel 1 goto failed

:install
".venv\Scripts\python.exe" -m pip --isolated --disable-pip-version-check install --no-index --no-cache-dir --only-binary=:all: --find-links=offline_packages --require-hashes -r requirements-offline.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -c "import mido,pynput,packaging,six"
if errorlevel 1 goto failed

:ready
if /i "%~1"=="--check" (
  echo Offline environment ready.
  exit /b 0
)
".venv\Scripts\python.exe" app.py
if errorlevel 1 goto failed
exit /b 0

:missing_python
echo Python 3.11+ with tkinter, venv and ensurepip is required.
echo Install the standard Windows Python distribution, then run start.bat again.
goto failed

:broken_environment
echo The local .venv is incompatible or damaged.
echo Close this app, rename .venv, then run start.bat again to recreate it.

:failed
echo Startup failed. See the error above. README.md contains setup instructions.
if /i not "%~1"=="--check" pause
exit /b 1
