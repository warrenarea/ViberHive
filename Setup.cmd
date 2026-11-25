@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
if defined VIRTUAL_ENV set "VIRTUAL_ENV="
if exist ".venv\Scripts\python.exe" goto :install
where py >nul 2>nul
if errorlevel 1 goto :try_python
for %%V in (3.13 3.14 3.12) do (
  py -%%V -I -c "import struct,platform,sys,sysconfig; sys.exit(struct.calcsize('P')!=8 or platform.machine().lower() not in ('amd64','x86_64') or bool(sysconfig.get_config_var('Py_GIL_DISABLED')))" >nul 2>nul
  if not errorlevel 1 (
    py -%%V -I -m venv ".venv"
    if errorlevel 1 goto :failed
    goto :install
  )
)
:try_python
python -I -c "import platform,sys,sysconfig; sys.exit(sys.version_info[:2] not in ((3,12),(3,13),(3,14)) or platform.machine().lower() not in ('amd64','x86_64') or bool(sysconfig.get_config_var('Py_GIL_DISABLED')))" >nul 2>nul
if errorlevel 1 goto :missing
python -I -m venv ".venv"
if errorlevel 1 goto :failed
:install
".venv\Scripts\python.exe" -I -B "%~dp0viberhive\shortcut.py"
if errorlevel 1 goto :failed
if /i "%~1"=="--shortcut" (
  echo ViberHive.lnk is ready to pin.
  exit /b 0
)
echo Installing verified Qt browser and image-processing packages.
".venv\Scripts\python.exe" -I -m pip --isolated --disable-pip-version-check --no-input install --index-url https://pypi.org/simple --require-hashes --only-binary=:all: --no-deps -r "requirements-win64.txt"
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -I -m pip --isolated --disable-pip-version-check check
if errorlevel 1 goto :failed
echo Checking the browser renderer, GIF decoder and video playback...
".venv\Scripts\python.exe" "%~dp0main.py" --check
if errorlevel 1 goto :failed
echo Installation complete. Open ViberHive.lnk to launch the picker or pin it to Start or the taskbar.
pause
exit /b 0
:missing
echo Install standard 64-bit CPython 3.13 from python.org, then run Setup.cmd again.
goto :stop
:failed
echo Setup did not finish. Review the error above.
:stop
pause
exit /b 1
